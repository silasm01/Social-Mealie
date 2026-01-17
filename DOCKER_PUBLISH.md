# Build and Push Docker Image

This guide explains how to build and publish the Docker image to a container registry.

## Option 1: GitHub Container Registry (Recommended)

### Automatic Builds with GitHub Actions

1. Push your code to GitHub
2. The GitHub Action will automatically build and push the image
3. Users can pull with:
   ```bash
   docker pull ghcr.io/YOUR_USERNAME/recipe-extractor:latest
   ```

### Manual Push to GitHub Container Registry

1. Create a Personal Access Token (PAT) with `write:packages` permission

2. Login to GitHub Container Registry:
   ```bash
   echo YOUR_GITHUB_TOKEN | docker login ghcr.io -u YOUR_USERNAME --password-stdin
   ```

3. Build the image:
   ```bash
   docker build -t ghcr.io/YOUR_USERNAME/recipe-extractor:latest .
   ```

4. Push the image:
   ```bash
   docker push ghcr.io/YOUR_USERNAME/recipe-extractor:latest
   ```

5. Make the package public (in GitHub package settings)

## Option 2: Docker Hub

### Push to Docker Hub

1. Login to Docker Hub:
   ```bash
   docker login
   ```

2. Build the image:
   ```bash
   docker build -t YOUR_DOCKERHUB_USERNAME/recipe-extractor:latest .
   ```

3. Push the image:
   ```bash
   docker push YOUR_DOCKERHUB_USERNAME/recipe-extractor:latest
   ```

4. Tag with version (optional):
   ```bash
   docker tag YOUR_DOCKERHUB_USERNAME/recipe-extractor:latest YOUR_DOCKERHUB_USERNAME/recipe-extractor:v1.0.0
   docker push YOUR_DOCKERHUB_USERNAME/recipe-extractor:v1.0.0
   ```

## Using the Pre-built Image

Once published, users can run without building:

```bash
# From GitHub Container Registry
docker run -d -p 5000:5000 ghcr.io/YOUR_USERNAME/recipe-extractor:latest

# From Docker Hub
docker run -d -p 5000:5000 YOUR_USERNAME/recipe-extractor:latest
```

Or update `docker-compose.yml` to use the pre-built image instead of building locally.
