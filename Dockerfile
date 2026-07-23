FROM maven:3.9.11-eclipse-temurin-21@sha256:6fdc855a6ed81d288ca7ca37ac6ff5e9308b612485c0801d70b25a858c83d237 AS author-build
WORKDIR /build
COPY java/pdf-author/pom.xml ./pom.xml
COPY java/pdf-author/src ./src
RUN mvn --batch-mode --no-transfer-progress package

FROM eclipse-temurin:21-jdk-jammy@sha256:9d8dcf999b0bce2453e913823595a5ff2a4e8e9e5d5241b45280d0ff069818ec AS verapdf-install
ARG VERAPDF_VERSION=1.30
ARG VERAPDF_PATCH=1
RUN apt-get update \
    && apt-get install --yes --no-install-recommends unzip wget \
    && rm -rf /var/lib/apt/lists/*
COPY docker/verapdf-install.xml /tmp/verapdf-install.xml
RUN wget --quiet --output-document=/tmp/verapdf.zip \
      "https://software.verapdf.org/releases/${VERAPDF_VERSION}/verapdf-greenfield-${VERAPDF_VERSION}.${VERAPDF_PATCH}-installer.zip" \
    && unzip -q /tmp/verapdf.zip -d /tmp \
    && java -jar "/tmp/verapdf-greenfield-${VERAPDF_VERSION}.${VERAPDF_PATCH}/verapdf-izpack-installer-${VERAPDF_VERSION}.${VERAPDF_PATCH}.jar" \
      /tmp/verapdf-install.xml

FROM eclipse-temurin:21-jre-jammy@sha256:d63bd8d9b171999cbed8576f2c76e874dd4856791a358536e5c4d407e77edc13
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
      fonts-dejavu-core poppler-utils python3 python3-pip python3-tomli \
      libgomp1 libglib2.0-0 libgl1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Pinned, CPU-only specialized recognition (PaddleOCR). The CPU paddlepaddle
# wheel keeps recognition GPU-free, and the weights are baked at build time so
# no model artifacts are downloaded at runtime. HOME is fixed so the container's
# arbitrary runtime uid resolves the same world-readable weight cache.
ENV HOME="/opt/accessibilizer"
# PaddlePaddle 2.6.x crashes in its SelfAttentionFusePass on otherwise
# supported x86-64 CPUs (including GitHub's hosted runners). 2.5.2 predates
# that regression and remains compatible with the pinned PaddleOCR release.
ARG PADDLEPADDLE_VERSION=2.5.2
ARG PADDLEOCR_VERSION=2.7.3
# numpy is pinned below 2 because the pinned paddlepaddle and opencv wheels are
# built against the NumPy 1 ABI ("_ARRAY_API not found" otherwise). Pin the
# headless OpenCV wheel to the version pip already resolves so cold builds do
# not download and reject several newer 50-60 MB candidates first.
RUN python3 -m pip install --no-cache-dir \
      --find-links https://www.paddlepaddle.org.cn/whl/linux/mkl/avx/stable.html \
      "numpy==1.26.4" \
      "opencv-python-headless==4.11.0.86" \
      "paddlepaddle==${PADDLEPADDLE_VERSION}" \
      "paddleocr==${PADDLEOCR_VERSION}"
COPY docker/paddle-warmup.py /tmp/paddle-warmup.py
RUN python3 /tmp/paddle-warmup.py \
    && rm -f /tmp/paddle-warmup.py \
    && chmod -R a+rX /opt/accessibilizer

# Pinned application dependencies: the human-editable YAML Review Record
# (PyYAML) and canonical-schema validation of it (jsonschema). Transitive
# dependencies (attrs, referencing, rpds-py, jsonschema-specifications) resolve
# to manylinux wheels, so the runtime stays offline and CPU-only.
RUN python3 -m pip install --no-cache-dir \
      "pyyaml==6.0.3" \
      "jsonschema==4.26.0"

COPY --from=author-build /build/target/pdf-author.jar /opt/accessibilizer/pdf-author.jar
COPY --from=verapdf-install /opt/verapdf /opt/verapdf
ARG ACCESSIBILIZER_SOURCE_REVISION
RUN test -n "${ACCESSIBILIZER_SOURCE_REVISION}" \
    && printf '%s\n' "${ACCESSIBILIZER_SOURCE_REVISION}" \
      > /opt/accessibilizer/source-revision
COPY src /opt/accessibilizer/src
COPY schemas /opt/accessibilizer/schemas
ENV PATH="/opt/verapdf:${PATH}" \
    ACCESSIBILIZER_CONTAINERIZED="1" \
    PYTHONPATH="/opt/accessibilizer/src" \
    ACCESSIBILIZER_PADDLE_WEIGHTS_VERSION="paddleocr-2.7.3-ppstructure-default"
WORKDIR /work
