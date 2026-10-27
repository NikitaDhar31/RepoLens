# Sandbox image for microsoft/AI-For-Beginners: the course notebooks plus JupyterLab, CPU only.
#
# Build (from this folder):  docker build -t repolens/ai-for-beginners:392d0df .
# Run:                       docker run --rm -p 8888:8888 repolens/ai-for-beginners:392d0df
#
# Two stages: the first downloads the course with git, the second is the image we keep. Only the
# files copied out of the first stage end up in the final image, so git never ships with it.

# --- Stage 1: fetch only the parts of the course we need, at a pinned commit -------------------
FROM python:3.11-slim-bookworm AS course

ARG COURSE_REPO=https://github.com/microsoft/AI-For-Beginners.git
ARG COURSE_COMMIT=392d0df1b2647cbee104942390551f1ed9e072c8

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Sparse checkout keeps the root files plus the listed folders and skips translations/ and
# translated_images/ (about 1.8 GB). blob:none downloads file contents only for those paths.
RUN git init -q /course \
    && cd /course \
    && git remote add origin "$COURSE_REPO" \
    && git sparse-checkout set lessons data .devcontainer \
    && git fetch -q --depth 1 --filter=blob:none origin "$COURSE_COMMIT" \
    && git checkout -q FETCH_HEAD \
    && rm -rf .git

# --- Stage 2: the sandbox image -----------------------------------------------------------------
FROM python:3.11-slim-bookworm

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Layers are ordered from least to most likely to change, so a rebuild reuses the slow ones.
# CPU-only PyTorch comes first: it is the biggest download and its version is pinned here.
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        torch==2.3.1+cpu torchvision==0.18.1+cpu

# The course's own package list, then ours. constraints.txt holds torch at the CPU build above,
# so nothing below can swap it for the multi-gigabyte CUDA build from PyPI.
COPY constraints.txt requirements-extra.txt /tmp/pip/
COPY --from=course /course/.devcontainer/requirements.txt /tmp/pip/course-requirements.txt
RUN pip install -c /tmp/pip/constraints.txt \
        -r /tmp/pip/course-requirements.txt -r /tmp/pip/requirements-extra.txt \
    && rm -rf /tmp/pip

# Notebooks run as an ordinary user, so code in them cannot change the system inside the container.
RUN useradd --create-home --uid 1000 learner
USER learner
WORKDIR /home/learner/course

# The course files change most often, so they come last.
COPY --from=course --chown=learner:learner /course /home/learner/course

# Hide TensorFlow's start-up messages about missing GPU drivers; this host has no GPU.
ENV TF_CPP_MIN_LOG_LEVEL=2

EXPOSE 8888

# Jupyter reads its login token from JUPYTER_TOKEN; without one it generates a random token.
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser"]
