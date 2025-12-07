# Use slim python image
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh

# Create virtual environment
RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# Install requirements
RUN pip install --upgrade pip

# Install Python dependencies first (layer caching)
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Install yt-dlp using YOUR REQUIRED EXACT COMMAND
RUN pip install -U "yt-dlp[default]"

# Copy project files
COPY . .

# Run the bot
CMD ["python3", "app.py"]
