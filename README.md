# TikTok & Instagram Recipe Extractor Web Application

A web application that extracts recipes from TikTok and Instagram Reels cooking videos using AI. **Process multiple videos simultaneously!**

## Features

- **Multi-platform support** - Works with both TikTok videos and Instagram Reels
- **Multi-video processing** - Add and process multiple videos at the same time
- **Collapsible list interface** - Each video is shown in a collapsible card
- **Real-time progress updates** - See live progress for each video independently  
- Simple web interface to paste video URLs
- Downloads and analyzes videos automatically
- Extracts audio transcription using Whisper
- Analyzes video frames with GPT-4 Vision
- Generates structured recipe in Schema.org format

## Quick Start with Docker (Recommended)

### Prerequisites
- Docker and Docker Compose installed
- That's it! No need to install Python, FFmpeg, or other dependencies

### Setup Environment Variables

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and add your OpenAI API key:
   ```bash
   OPENAI_API_KEY=your_actual_openai_api_key_here
   ```

### Option A: Pull Pre-built Image (Easiest)

If the image is published to a registry, you can pull and run directly:

```bash
# From GitHub Container Registry (replace YOUR_USERNAME)
docker run -d -p 5000:5000 --env-file .env --name recipe-extractor ghcr.io/YOUR_USERNAME/recipe-extractor:latest

# OR from Docker Hub (replace YOUR_USERNAME)
docker run -d -p 5000:5000 --env-file .env --name recipe-extractor YOUR_USERNAME/recipe-extractor:latest
```

Then open http://localhost:5000

### Option B: Build Locally

1. Clone or download this repository

2. Create your `.env` file (see above)

3. Start the application:
```bash
docker-compose up -d
```

3. Open your browser and go to:
```
http://localhost:5000
```

4. To stop the application:
```bash
docker-compose down
```

### Update to Latest Version

If using a pre-built image:
```bash
docker pull ghcr.io/YOUR_USERNAME/recipe-extractor:latest
docker-compose up -d
```

### Run with Docker (without compose)

**Using pre-built image:**
```bash
docker run -d -p 5000:5000 --env-file .env --name recipe-extractor ghcr.io/YOUR_USERNAME/recipe-extractor:latest
```

**Building locally:**

1. Build the image:
```bash
docker build -t recipe-extractor .
```

2. Run the container:
```Publishing the Docker Image

If you want to publish this image for others to use, see [DOCKER_PUBLISH.md](DOCKER_PUBLISH.md) for instructions on:
- Publishing to GitHub Container Registry
- Publishing to Docker Hub
- Setting up automatic builds with GitHub Actions

## bash
docker run -d -p 5000:5000 --name recipe-extractor recipe-extractor
```

3. View logs:
```bash
docker logs -f recipe-extractor
```

4. Stop the container:
```bash
docker stop recipe-extractor
docker rm recipe-extractor
```

## Manual Installation (Alternative)

If you prefer not to use Docker:

1. Copy the environment file:
   ```bash
   cp .env.example .env
   ```

3. Edit `.env` and add your OpenAI API key

3. Install Python dependencies:
```bash
pip install -r requirements.txt
4``

2. Make sure you have FFmpeg installed on your system:
   - Windows: Download from https://ffmpeg.org/download.html
   - Linux: `sudo apt-get install ffmpeg`
   - macOS: `brew install ffmpeg`

3. Start the web server:
```bash
python app.py
```

## Usage

1. Open your browser and go to `http://localhost:5000`

2. Paste a TikTok or Instagram Reels URL and click "Add Video"

3. Add more videos to process them concurrently!

4. Click on any video card to expand/collapse and see its progress

5. The recipe will be displayed in JSON format when complete

## Supported Platforms

- **TikTok** - Any public TikTok cooking video
- **Instagram Reels** - Public Instagram Reels (requires the direct link to the reel)

## How It Works

1. Downloads the video (TikTok via pyktok, Instagram via instaloader)
2. Extracts the caption/description
3. Checks if the description contains complete instructions
4. If incomplete, extracts frames (1 per second)
5. Transcribes audio using Whisper
6. Analyzes each frame with GPT-4 Vision
7. Combines all information to generate a structured recipe
8. Posts the recipe to an external API (optional)

Each video is processed independently, allowing you to queue multiple videos and monitor their progress simultaneously.

## Configuration

### Environment Variables

All sensitive configuration is managed through the `.env` file:

- **OPENAI_API_KEY** (required) - Your OpenAI API key for GPT-4 and Whisper
- **RECIPE_API_URL** (optional) - External API endpoint for posting recipes
- **RECIPE_API_TOKEN** (optional) - Bearer token for external API authentication
- **FLASK_ENV** (optional) - Flask environment (production/development)
- **FLASK_DEBUG** (optional) - Enable Flask debug mode (True/False)

**Important:** Never commit your `.env` file to version control. Use `.env.example` as a template.

## Docker Configuration

The Docker setup includes:
- **Automatic FFmpeg installation** - No manual setup required
- **Port 5000 exposed** - Access the web interface
- **Resource limits** - Configurable CPU and memory limits
- **Automatic restart** - Container restarts unless stopped
- **Volume mounting** - Temporary files stored efficiently

## Troubleshooting

### Docker Issues

**Port already in use:**
```bash
# Use a different port
docker run -d -p 8080:5000 --name recipe-extractor recipe-extractor
```

**Check container logs:**
```bash
docker logs recipe-extractor
```

**Restart container:**
```bash
docker restart recipe-extractor
```

### General Issues

- Make sure you have a stable internet connection
- Ensure the video URLs are public and accessible
- Check that you have sufficient disk space for temporary video files
