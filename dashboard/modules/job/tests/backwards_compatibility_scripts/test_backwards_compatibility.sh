#!/usr/bin/env bash

unset RAY_ADDRESS

if ! [ -x "$(command -v conda)" ]; then
    echo "conda doesn't exist. Please download conda for this machine"
    exit 1
else
    echo "conda exists"
fi

pip install --upgrade pip

# This is required to use conda activate
source "$(conda info --base)/etc/profile.d/conda.sh"

PYTHON_VERSION=$(python -c"from platform import python_version; print(python_version())")

RAY_VERSIONS=("2.0.1")

for RAY_VERSION in "${RAY_VERSIONS[@]}"
do
    env_name=${JOB_COMPATIBILITY_TEST_TEMP_ENV}

    # Clean up if env name is already taken from previous leaking runs
    conda env remove --name="${env_name}"

    printf "\n\n\n"
    echo "========================================================================================="
    printf "Creating new conda environment with python %s for ray %s \n" "${PYTHON_VERSION}" "${RAY_VERSION}"
    echo "========================================================================================="
    printf "\n\n\n"

    conda create -y -n "${env_name}" python="${PYTHON_VERSION}"
    conda activate "${env_name}"

    # Pin pydantic version due to: https://github.com/ray-project/ray/issues/36990.
    pip install -U "pydantic<2" ray=="${RAY_VERSION}" ray[default]=="${RAY_VERSION}"

    printf "\n\n\n"
    echo "========================================================="
    printf "Installed ray job server version: "
    SERVER_RAY_VERSION=$(python -c "import ray; print(ray.__version__)")
    printf "%s \n" "${SERVER_RAY_VERSION}"
    echo "========================================================="
    printf "\n\n\n"
    ray stop --force
    ray start --head

    conda deactivate

    CLIENT_RAY_VERSION=$(python -c "import ray; print(ray.__version__)")
    CLIENT_RAY_COMMIT=$(python -c "import ray; print(ray.__commit__)")
    printf "\n\n\n"
    echo "========================================================================================="
    printf "Using Ray %s on %s as job client \n" "${CLIENT_RAY_VERSION}" "${CLIENT_RAY_COMMIT}"
    echo "========================================================================================="
    printf "\n\n\n"

    export RAY_ADDRESS="http://127.0.0.1:8265"

    cleanup () {
        unset RAY_ADDRESS
        ray stop --force
        conda remove -y --name "${env_name}" --all
    }

    JOB_ID=$(python -c "import uuid; print(uuid.uuid4().hex)")

    # Get directory of current file. https://stackoverflow.com/questions/59895/
    DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

    if ! ray job submit --job-id="${JOB_ID}" --working-dir="${DIR}" --runtime-env-json='{"pip": ["requests==2.26.0"]}' -- python script.py; then
        cleanup
        exit 1
    fi

    if ! ray job status "${JOB_ID}"; then
        cleanup
        exit 1
    fi

    if ! ray job logs "${JOB_ID}"; then
        cleanup
        exit 1
    fi

    if ! pytest -vs "${DIR}"/../test_backwards_compatibility.py::test_error_message; then
        cleanup
        exit 1
    fi

    cleanup
done
