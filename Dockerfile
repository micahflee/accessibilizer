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
RUN apt-get update \
    && apt-get install --yes --no-install-recommends fonts-dejavu-core poppler-utils python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=author-build /build/target/pdf-author.jar /opt/accessibilizer/pdf-author.jar
COPY --from=verapdf-install /opt/verapdf /opt/verapdf
COPY src /opt/accessibilizer/src
ENV PATH="/opt/verapdf:${PATH}" \
    PYTHONPATH="/opt/accessibilizer/src"
WORKDIR /work
