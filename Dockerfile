FROM python:3.11-slim

# Evitar prompts interactivos durante la instalación
ENV DEBIAN_FRONTEND=noninteractive

# Instalar dependencias del sistema y herramientas esenciales
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    apt-transport-https \
    lsb-release \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Instalar Azure CLI desde los repositorios oficiales de Microsoft
RUN mkdir -p /etc/apt/keyrings \
    && curl -sLS https://packages.microsoft.com/keys/microsoft.asc | \
       gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
    && chmod go+r /etc/apt/keyrings/microsoft.gpg \
    && AZ_REPO=$(lsb_release -cs) \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ $AZ_REPO main" > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y azure-cli \
    && rm -rf /var/lib/apt/lists/*

# Instalar kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl

# Instalar kubelogin (Plugin de autenticación de K8s para Azure)
RUN az aks install-cli --client-version latest --install-location /usr/local/bin/kubectl-default --kubelogin-install-location /usr/local/bin/kubelogin \
    && rm -f /usr/local/bin/kubectl-default

# Establecer directorio de trabajo
WORKDIR /app

# Copiar dependencias de Python
COPY requirements.txt .

# Instalar librerías de Python
RUN pip install --no-cache-dir -r requirements.txt

# Instalar navegadores de Playwright (y sus dependencias de SO)
RUN playwright install --with-deps chromium

# Copiar el resto de la aplicación
COPY . .

# Exponer el puerto de Uvicorn
EXPOSE 8088

# Ejecutar el servidor web
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8088"]
