import inspect
import json
import logging
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, Set

import pydantic
from google.protobuf.json_format import MessageToDict
from pydantic import (
    BaseModel,
    NonNegativeFloat,
    PositiveFloat,
    NonNegativeInt,
    PositiveInt,
    validator,
    Field,
)

from ray import cloudpickle
from ray.util.placement_group import VALID_PLACEMENT_GROUP_STRATEGIES

from ray.serve._private.constants import (
    DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_S,
    DEFAULT_GRACEFUL_SHUTDOWN_WAIT_LOOP_S,
    DEFAULT_GRPC_PORT,
    DEFAULT_HEALTH_CHECK_PERIOD_S,
    DEFAULT_HEALTH_CHECK_TIMEOUT_S,
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PORT,
    DEFAULT_MAX_CONCURRENT_QUERIES,
    DEFAULT_UVICORN_KEEP_ALIVE_TIMEOUT_S,
    SERVE_LOGGER_NAME,
)
from ray.serve._private.utils import DEFAULT, DeploymentOptionUpdateType
from ray.serve.generated.serve_pb2 import (
    DeploymentConfig as DeploymentConfigProto,
    DeploymentLanguage,
    AutoscalingConfig as AutoscalingConfigProto,
    ReplicaConfig as ReplicaConfigProto,
)
from ray._private import ray_option_utils
from ray._private.utils import import_attr, resources_from_ray_options
from ray._private.serialization import pickle_dumps
from ray.util.annotations import DeveloperAPI, PublicAPI

logger = logging.getLogger(SERVE_LOGGER_NAME)


@PublicAPI(stability="stable")
class AutoscalingConfig(BaseModel):
    # Please keep these options in sync with those in
    # `src/ray/protobuf/serve.proto`.

    # Publicly exposed options
    min_replicas: NonNegativeInt = 1
    initial_replicas: Optional[NonNegativeInt] = None
    max_replicas: PositiveInt = 1
    target_num_ongoing_requests_per_replica: PositiveFloat = 1.0

    # Private options below.

    # Metrics scraping options

    # How often to scrape for metrics
    metrics_interval_s: PositiveFloat = 10.0
    # Time window to average over for metrics.
    look_back_period_s: PositiveFloat = 30.0

    # Internal autoscaling configuration options

    # Multiplicative "gain" factor to limit scaling decisions
    smoothing_factor: PositiveFloat = 1.0

    # How frequently to make autoscaling decisions
    # loop_period_s: float = CONTROL_LOOP_PERIOD_S
    # How long to wait before scaling down replicas
    downscale_delay_s: NonNegativeFloat = 600.0
    # How long to wait before scaling up replicas
    upscale_delay_s: NonNegativeFloat = 30.0

    @validator("max_replicas", always=True)
    def replicas_settings_valid(cls, max_replicas, values):
        min_replicas = values.get("min_replicas")
        initial_replicas = values.get("initial_replicas")
        if min_replicas is not None and max_replicas < min_replicas:
            raise ValueError(
                f"max_replicas ({max_replicas}) must be greater than "
                f"or equal to min_replicas ({min_replicas})!"
            )

        if initial_replicas is not None:
            if initial_replicas < min_replicas:
                raise ValueError(
                    f"min_replicas ({min_replicas}) must be less than "
                    f"or equal to initial_replicas ({initial_replicas})!"
                )
            elif initial_replicas > max_replicas:
                raise ValueError(
                    f"max_replicas ({max_replicas}) must be greater than "
                    f"or equal to initial_replicas ({initial_replicas})!"
                )

        return max_replicas

    # TODO(architkulkarni): implement below
    # The num_ongoing_requests_per_replica error ratio (desired / current)
    # threshold for overriding `upscale_delay_s`
    # panic_mode_threshold: float = 2.0

    # TODO(architkulkarni): Add reasonable defaults


def _needs_pickle(deployment_language: DeploymentLanguage, is_cross_language: bool):
    """From Serve client API's perspective, decide whether pickling is needed."""
    if deployment_language == DeploymentLanguage.PYTHON and not is_cross_language:
        # Python client deploying Python replicas.
        return True
    elif deployment_language == DeploymentLanguage.JAVA and is_cross_language:
        # Python client deploying Java replicas,
        # using xlang serialization via cloudpickle.
        return True
    else:
        return False


@PublicAPI(stability="stable")
class DeploymentConfig(BaseModel):
    """Configuration options for a deployment, to be set by the user.

    Args:
        num_replicas (Optional[int]): The number of processes to start up that
            will handle requests to this deployment. Defaults to 1.
        max_concurrent_queries (Optional[int]): The maximum number of queries
            that will be sent to a replica of this deployment without receiving
            a response. Defaults to 100.
        user_config (Optional[Any]): Arguments to pass to the reconfigure
            method of the deployment. The reconfigure method is called if
            user_config is not None. Must be json-serializable.
        graceful_shutdown_wait_loop_s (Optional[float]): Duration
            that deployment replicas will wait until there is no more work to
            be done before shutting down.
        graceful_shutdown_timeout_s (Optional[float]):
            Controller waits for this duration to forcefully kill the replica
            for shutdown.
        health_check_period_s (Optional[float]):
            Frequency at which the controller will health check replicas.
        health_check_timeout_s (Optional[float]):
            Timeout that the controller will wait for a response from the
            replica's health check before marking it unhealthy.
        user_configured_option_names (Set[str]):
            The names of options manually configured by the user.
    """

    num_replicas: NonNegativeInt = Field(
        default=1, update_type=DeploymentOptionUpdateType.LightWeight
    )
    max_concurrent_queries: Optional[int] = Field(
        default=None, update_type=DeploymentOptionUpdateType.NeedsReconfigure
    )
    user_config: Any = Field(
        default=None, update_type=DeploymentOptionUpdateType.NeedsActorReconfigure
    )

    graceful_shutdown_timeout_s: NonNegativeFloat = Field(
        default=DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT_S,
        update_type=DeploymentOptionUpdateType.NeedsReconfigure,
    )
    graceful_shutdown_wait_loop_s: NonNegativeFloat = Field(
        default=DEFAULT_GRACEFUL_SHUTDOWN_WAIT_LOOP_S,
        update_type=DeploymentOptionUpdateType.NeedsActorReconfigure,
    )

    health_check_period_s: PositiveFloat = Field(
        default=DEFAULT_HEALTH_CHECK_PERIOD_S,
        update_type=DeploymentOptionUpdateType.NeedsReconfigure,
    )
    health_check_timeout_s: PositiveFloat = Field(
        default=DEFAULT_HEALTH_CHECK_TIMEOUT_S,
        update_type=DeploymentOptionUpdateType.NeedsReconfigure,
    )

    autoscaling_config: Optional[AutoscalingConfig] = Field(
        default=None, update_type=DeploymentOptionUpdateType.LightWeight
    )

    # This flag is used to let replica know they are deployed from
    # a different language.
    is_cross_language: bool = False

    # This flag is used to let controller know which language does
    # the deploymnent use.
    deployment_language: Any = DeploymentLanguage.PYTHON

    version: Optional[str] = Field(
        default=None,
        update_type=DeploymentOptionUpdateType.HeavyWeight,
    )

    # Contains the names of deployment options manually set by the user
    user_configured_option_names: Set[str] = set()

    class Config:
        validate_assignment = True
        extra = "forbid"
        arbitrary_types_allowed = True

    # Dynamic default for max_concurrent_queries
    @validator("max_concurrent_queries", always=True)
    def set_max_queries_by_mode(cls, v, values):  # noqa 805
        if v is None:
            v = DEFAULT_MAX_CONCURRENT_QUERIES
        else:
            if v <= 0:
                raise ValueError("max_concurrent_queries must be >= 0")
        return v

    @validator("user_config", always=True)
    def user_config_json_serializable(cls, v):
        if isinstance(v, bytes):
            return v
        if v is not None:
            try:
                json.dumps(v)
            except TypeError as e:
                raise ValueError(f"user_config is not JSON-serializable: {str(e)}.")

        return v

    def needs_pickle(self):
        return _needs_pickle(self.deployment_language, self.is_cross_language)

    def to_proto(self):
        data = self.dict()
        if data.get("user_config") is not None:
            if self.needs_pickle():
                data["user_config"] = cloudpickle.dumps(data["user_config"])
        if data.get("autoscaling_config"):
            data["autoscaling_config"] = AutoscalingConfigProto(
                **data["autoscaling_config"]
            )
        data["user_configured_option_names"] = list(
            data["user_configured_option_names"]
        )
        return DeploymentConfigProto(**data)

    def to_proto_bytes(self):
        return self.to_proto().SerializeToString()

    @classmethod
    def from_proto(cls, proto: DeploymentConfigProto):
        data = MessageToDict(
            proto,
            including_default_value_fields=True,
            preserving_proto_field_name=True,
            use_integers_for_enums=True,
        )
        if "user_config" in data:
            if data["user_config"] != "":
                deployment_language = (
                    data["deployment_language"]
                    if "deployment_language" in data
                    else DeploymentLanguage.PYTHON
                )
                is_cross_language = (
                    data["is_cross_language"] if "is_cross_language" in data else False
                )
                needs_pickle = _needs_pickle(deployment_language, is_cross_language)
                if needs_pickle:
                    data["user_config"] = cloudpickle.loads(proto.user_config)
                else:
                    # after MessageToDict, bytes data has been deal with base64
                    data["user_config"] = proto.user_config
            else:
                data["user_config"] = None
        if "autoscaling_config" in data:
            data["autoscaling_config"] = AutoscalingConfig(**data["autoscaling_config"])
        if "version" in data:
            if data["version"] == "":
                data["version"] = None
        if "user_configured_option_names" in data:
            data["user_configured_option_names"] = set(
                data["user_configured_option_names"]
            )
        return cls(**data)

    @classmethod
    def from_proto_bytes(cls, proto_bytes: bytes):
        proto = DeploymentConfigProto.FromString(proto_bytes)
        return cls.from_proto(proto)

    @classmethod
    def from_default(cls, **kwargs):
        """Creates a default DeploymentConfig and overrides it with kwargs.

        Ignores any kwargs set to DEFAULT.VALUE.

        Raises:
            TypeError: when a keyword that's not an argument to the class is
                passed in.
        """

        config = cls()
        valid_config_options = set(config.dict().keys())

        # Friendly error if a non-DeploymentConfig kwarg was passed in
        for key, val in kwargs.items():
            if key not in valid_config_options:
                raise TypeError(
                    f'Got invalid Deployment config option "{key}" '
                    f"(with value {val}) as keyword argument. All Deployment "
                    "config options must come from this list: "
                    f"{list(valid_config_options)}."
                )

        kwargs = {key: val for key, val in kwargs.items() if val != DEFAULT.VALUE}

        for key, val in kwargs.items():
            config.__setattr__(key, val)

        return config


@DeveloperAPI
class ReplicaConfig:
    """Configuration for a deployment's replicas.

    Provides five main properties (see property docstrings for more info):
        deployment_def: the code, or a reference to the code, that this
            replica should run.
        init_args: the deployment_def's init_args.
        init_kwargs: the deployment_def's init_kwargs.
        ray_actor_options: the Ray actor options to pass into the replica's
            actor.
        resource_dict: contains info on this replica's actor's resource needs.

    Offers a serialized equivalent (e.g. serialized_deployment_def) for
    deployment_def, init_args, and init_kwargs. Deserializes these properties
    when they're first accessed, if they were not passed in directly through
    create().

    Use the classmethod create() to make a ReplicaConfig with the deserialized
    properties.

    Note: overwriting or setting any property after the ReplicaConfig has been
    constructed is currently undefined behavior. The config's fields should not
    be modified externally after it is created.
    """

    def __init__(
        self,
        deployment_def_name: str,
        serialized_deployment_def: bytes,
        serialized_init_args: bytes,
        serialized_init_kwargs: bytes,
        ray_actor_options: Dict,
        placement_group_bundles: Optional[List[Dict[str, float]]] = None,
        placement_group_strategy: Optional[str] = None,
        needs_pickle: bool = True,
    ):
        """Construct a ReplicaConfig with serialized properties.

        All parameters are required. See classmethod create() for defaults.
        """
        self.deployment_def_name = deployment_def_name

        # Store serialized versions of code properties.
        self.serialized_deployment_def = serialized_deployment_def
        self.serialized_init_args = serialized_init_args
        self.serialized_init_kwargs = serialized_init_kwargs

        # Deserialize properties when first accessed. See @property methods.
        self._deployment_def = None
        self._init_args = None
        self._init_kwargs = None

        # Configure ray_actor_options. These are the Ray options ultimately
        # passed into the replica's actor when it's created.
        self.ray_actor_options = ray_actor_options
        self._validate_ray_actor_options()

        self.placement_group_bundles = placement_group_bundles
        self.placement_group_strategy = placement_group_strategy
        self._validate_placement_group_options()

        # Create resource_dict. This contains info about the replica's resource
        # needs. It does NOT set the replica's resource usage. That's done by
        # the ray_actor_options.
        self.resource_dict = resources_from_ray_options(self.ray_actor_options)
        self.needs_pickle = needs_pickle

    def update_ray_actor_options(self, ray_actor_options):
        self.ray_actor_options = ray_actor_options
        self._validate_ray_actor_options()
        self.resource_dict = resources_from_ray_options(self.ray_actor_options)

    def update_placement_group_options(
        self,
        placement_group_bundles: Optional[List[Dict[str, float]]],
        placement_group_strategy: Optional[str],
    ):
        self.placement_group_bundles = placement_group_bundles
        self.placement_group_strategy = placement_group_strategy
        self._validate_placement_group_options()

    @classmethod
    def create(
        cls,
        deployment_def: Union[Callable, str],
        init_args: Optional[Union[Tuple[Any], bytes]] = None,
        init_kwargs: Optional[Dict[Any, Any]] = None,
        ray_actor_options: Optional[Dict] = None,
        placement_group_bundles: Optional[List[Dict[str, float]]] = None,
        placement_group_strategy: Optional[str] = None,
        deployment_def_name: Optional[str] = None,
    ):
        """Create a ReplicaConfig from deserialized parameters."""

        if inspect.isfunction(deployment_def):
            if init_args:
                raise ValueError("init_args not supported for function deployments.")
            elif init_kwargs:
                raise ValueError("init_kwargs not supported for function deployments.")

        if not isinstance(deployment_def, (Callable, str)):
            raise TypeError(
                f'Got invalid type "{type(deployment_def)}" for '
                "deployment_def. Expected deployment_def to be a "
                "class, function, or string."
            )
        # Set defaults
        if init_args is None:
            init_args = ()
        if init_kwargs is None:
            init_kwargs = {}
        if ray_actor_options is None:
            ray_actor_options = {}
        if deployment_def_name is None:
            if isinstance(deployment_def, str):
                deployment_def_name = deployment_def
            else:
                deployment_def_name = deployment_def.__name__

        config = cls(
            deployment_def_name,
            pickle_dumps(
                deployment_def,
                f"Could not serialize the deployment {repr(deployment_def)}",
            ),
            pickle_dumps(init_args, "Could not serialize the deployment init args"),
            pickle_dumps(init_kwargs, "Could not serialize the deployment init kwargs"),
            ray_actor_options,
            placement_group_bundles,
            placement_group_strategy,
        )

        config._deployment_def = deployment_def
        config._init_args = init_args
        config._init_kwargs = init_kwargs

        return config

    def _validate_ray_actor_options(self):
        if not isinstance(self.ray_actor_options, dict):
            raise TypeError(
                f'Got invalid type "{type(self.ray_actor_options)}" for '
                "ray_actor_options. Expected a dictionary."
            )
        # Please keep this in sync with the docstring for the ray_actor_options
        # kwarg in api.py.
        allowed_ray_actor_options = {
            # Resource options
            "accelerator_type",
            "memory",
            "num_cpus",
            "num_gpus",
            "object_store_memory",
            "resources",
            # Other options
            "runtime_env",
        }

        for option in self.ray_actor_options:
            if option not in allowed_ray_actor_options:
                raise ValueError(
                    f"Specifying '{option}' in ray_actor_options is not allowed. "
                    f"Allowed options: {allowed_ray_actor_options}"
                )
        ray_option_utils.validate_actor_options(self.ray_actor_options, in_options=True)

        # Set Serve replica defaults
        if self.ray_actor_options.get("num_cpus") is None:
            self.ray_actor_options["num_cpus"] = 1

    def _validate_placement_group_options(self) -> None:
        if (
            self.placement_group_strategy is not None
            and self.placement_group_strategy not in VALID_PLACEMENT_GROUP_STRATEGIES
        ):
            raise ValueError(
                f"Invalid placement group strategy '{self.placement_group_strategy}'. "
                f"Supported strategies are: {VALID_PLACEMENT_GROUP_STRATEGIES}."
            )

        if (
            self.placement_group_strategy is not None
            and self.placement_group_bundles is None
        ):
            raise ValueError(
                "If `placement_group_strategy` is provided, `placement_group_bundles` "
                "must also be provided."
            )

        if self.placement_group_bundles is not None:
            if (
                not isinstance(self.placement_group_bundles, list)
                or len(self.placement_group_bundles) == 0
            ):
                raise ValueError(
                    "`placement_group_bundles` must be a non-empty list of resource "
                    'dictionaries. For example: `[{"CPU": 1.0}, {"GPU": 1.0}]`.'
                )

            for i, bundle in enumerate(self.placement_group_bundles):
                if (
                    not isinstance(bundle, dict)
                    or not all(isinstance(k, str) for k in bundle.keys())
                    or not all(isinstance(v, (int, float)) for v in bundle.values())
                ):
                    raise ValueError(
                        "`placement_group_bundles` must be a non-empty list of "
                        "resource dictionaries. For example: "
                        '`[{"CPU": 1.0}, {"GPU": 1.0}]`.'
                    )

                # Validate that the replica actor fits in the first bundle.
                if i == 0:
                    bundle_cpu = bundle.get("CPU", 0)
                    replica_actor_num_cpus = self.ray_actor_options.get("num_cpus", 0)
                    if bundle_cpu < replica_actor_num_cpus:
                        raise ValueError(
                            "When using `placement_group_bundles`, the replica actor "
                            "will be placed in the first bundle, so the resource "
                            "requirements for the actor must be a subset of the first "
                            "bundle. `num_cpus` for the actor is "
                            f"{replica_actor_num_cpus} but the bundle only has "
                            f"{bundle_cpu} `CPU` specified."
                        )

                    bundle_gpu = bundle.get("GPU", 0)
                    replica_actor_num_gpus = self.ray_actor_options.get("num_gpus", 0)
                    if bundle_gpu < replica_actor_num_gpus:
                        raise ValueError(
                            "When using `placement_group_bundles`, the replica actor "
                            "will be placed in the first bundle, so the resource "
                            "requirements for the actor must be a subset of the first "
                            "bundle. `num_gpus` for the actor is "
                            f"{replica_actor_num_gpus} but the bundle only has "
                            f"{bundle_gpu} `GPU` specified."
                        )

                    replica_actor_resources = self.ray_actor_options.get(
                        "resources", {}
                    )
                    for actor_resource, actor_value in replica_actor_resources.items():
                        bundle_value = bundle.get(actor_resource, 0)
                        if bundle_value < actor_value:
                            raise ValueError(
                                "When using `placement_group_bundles`, the replica "
                                "actor will be placed in the first bundle, so the "
                                "resource requirements for the actor must be a subset "
                                f"of the first bundle. `{actor_resource}` requirement "
                                f"for the actor is {actor_value} but the bundle only "
                                f"has {bundle_value} `{actor_resource}` specified."
                            )

    @property
    def deployment_def(self) -> Union[Callable, str]:
        """The code, or a reference to the code, that this replica runs.

        For Python replicas, this can be one of the following:
            - Function (Callable)
            - Class (Callable)
            - Import path (str)

        For Java replicas, this can be one of the following:
            - Class path (str)
        """
        if self._deployment_def is None:
            if self.needs_pickle:
                self._deployment_def = cloudpickle.loads(self.serialized_deployment_def)
            else:
                self._deployment_def = self.serialized_deployment_def.decode(
                    encoding="utf-8"
                )

        return self._deployment_def

    @property
    def init_args(self) -> Optional[Union[Tuple[Any], bytes]]:
        """The init_args for a Python class.

        This property is only meaningful if deployment_def is a Python class.
        Otherwise, it is None.
        """
        if self._init_args is None:
            if self.needs_pickle:
                self._init_args = cloudpickle.loads(self.serialized_init_args)
            else:
                self._init_args = self.serialized_init_args

        return self._init_args

    @property
    def init_kwargs(self) -> Optional[Tuple[Any]]:
        """The init_kwargs for a Python class.

        This property is only meaningful if deployment_def is a Python class.
        Otherwise, it is None.
        """

        if self._init_kwargs is None:
            self._init_kwargs = cloudpickle.loads(self.serialized_init_kwargs)

        return self._init_kwargs

    @classmethod
    def from_proto(cls, proto: ReplicaConfigProto, needs_pickle: bool = True):
        return ReplicaConfig(
            proto.deployment_def_name,
            proto.deployment_def,
            proto.init_args if proto.init_args != b"" else None,
            proto.init_kwargs if proto.init_kwargs != b"" else None,
            json.loads(proto.ray_actor_options),
            json.loads(proto.placement_group_bundles)
            if proto.placement_group_bundles
            else None,
            proto.placement_group_strategy
            if proto.placement_group_strategy != ""
            else None,
            needs_pickle,
        )

    @classmethod
    def from_proto_bytes(cls, proto_bytes: bytes, needs_pickle: bool = True):
        proto = ReplicaConfigProto.FromString(proto_bytes)
        return cls.from_proto(proto, needs_pickle)

    def to_proto(self):
        return ReplicaConfigProto(
            deployment_def_name=self.deployment_def_name,
            deployment_def=self.serialized_deployment_def,
            init_args=self.serialized_init_args,
            init_kwargs=self.serialized_init_kwargs,
            ray_actor_options=json.dumps(self.ray_actor_options),
            placement_group_bundles=json.dumps(self.placement_group_bundles)
            if self.placement_group_bundles is not None
            else "",
            placement_group_strategy=self.placement_group_strategy,
        )

    def to_proto_bytes(self):
        return self.to_proto().SerializeToString()


# Keep in sync with ServeDeploymentMode in dashboard/client/src/type/serve.ts
@DeveloperAPI
class DeploymentMode(str, Enum):
    NoServer = "NoServer"
    HeadOnly = "HeadOnly"
    EveryNode = "EveryNode"
    FixedNumber = "FixedNumber"


@PublicAPI(stability="beta")
class HTTPOptions(pydantic.BaseModel):
    # Documentation inside serve.start for user's convenience.
    host: Optional[str] = DEFAULT_HTTP_HOST
    port: int = DEFAULT_HTTP_PORT
    middlewares: List[Any] = []
    location: Optional[DeploymentMode] = DeploymentMode.HeadOnly
    num_cpus: int = 0
    root_url: str = ""
    root_path: str = ""
    fixed_number_replicas: Optional[int] = None
    fixed_number_selection_seed: int = 0
    request_timeout_s: Optional[float] = None
    keep_alive_timeout_s: int = DEFAULT_UVICORN_KEEP_ALIVE_TIMEOUT_S

    @validator("location", always=True)
    def location_backfill_no_server(cls, v, values):
        if values["host"] is None or v is None:
            return DeploymentMode.NoServer
        return v

    @validator("fixed_number_replicas", always=True)
    def fixed_number_replicas_should_exist(cls, v, values):
        if values["location"] == DeploymentMode.FixedNumber and v is None:
            raise ValueError(
                "When location='FixedNumber', you must specify "
                "the `fixed_number_replicas` parameter."
            )
        return v

    class Config:
        validate_assignment = True
        extra = "forbid"
        arbitrary_types_allowed = True


@PublicAPI(stability="beta")
class gRPCOptions(BaseModel):
    """Configuration options for gRPC proxy.

    Args:
        port (int):
            Port for gRPC server if started. Default to 9000. Cannot be
            updated once Serve has started running. Serve must be shut down and
            restarted with the new port instead.
        grpc_servicer_functions (List[str]):
            The servicer functions used to add the method handlers to the gRPC server.
            Default to empty list, which means no gRPC methods will be added
            and no gRPC server will be started. The servicer functions need to be
            importable from the context of where Serve is running.
    """

    port: int = DEFAULT_GRPC_PORT
    grpc_servicer_functions: List[str] = []

    @property
    def grpc_servicer_func_callable(self) -> List[Callable]:
        """Return a list of callable functions from the grpc_servicer_functions.

        If the function is not callable or not found, it will be ignored and a warning
        will be logged.
        """
        callables = []
        for func in self.grpc_servicer_functions:
            try:
                imported_func = import_attr(func)
                if callable(imported_func):
                    callables.append(imported_func)
                else:
                    logger.warning(
                        f"{func} is not a callable function! Please make sure "
                        "the function is imported correctly."
                    )
            except ModuleNotFoundError:
                logger.warning(
                    f"{func} can't be imported! Please make sure there are no typo "
                    "in those functions. Or you might want to rebuild service "
                    "definitions if .proto file is changed."
                )

        return callables
