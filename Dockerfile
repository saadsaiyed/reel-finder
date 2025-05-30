# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install waitress

# Copy the current directory contents into the container at /app
COPY . /app

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Define environment variable to ensure output is sent to terminal
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["waitress-serve", "--port=5000", "app:app"]

# # First stage: build the application and its dependencies 
# FROM python:3.9-slim as builder

# WORKDIR /app 

# COPY requirements.txt . 

# RUN pip install --no-cache-dir -r requirements.txt 

# COPY . . 

# # Second stage: create a minimal runtime image 
# FROM python:3.9-slim-buster 

# WORKDIR /app 

# # Copy only the necessary files from the builder stage 
# COPY --from=builder /app/app.py /app/app.py 
# COPY --from=builder /app/templates /app/templates 
# COPY --from=builder /app/venv/Lib/site-packages /app/lib/python3.9/site-packages 

# # Install only the runtime dependencies 
# RUN pip install --no-cache-dir waitress 

# EXPOSE 5000 

# ENV PYTHONUNBUFFERED=1 

# CMD ["waitress-serve", "--port=5000", "app:app"]