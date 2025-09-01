# Use an official Python runtime as a parent image
FROM python:3.9-slim-buster

# Set the working directory in the container
WORKDIR /app

# Install system dependencies for a shell environment
RUN apt-get update && apt-get install -y \
    bash \
    git \
    nodejs \
    npm \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

# Copy the local directory contents into the container
COPY . /app

# Install any needed Python packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Run the app.py when the container launches
CMD ["python", "app.py"]
