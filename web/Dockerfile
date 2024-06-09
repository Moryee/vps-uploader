# Python image
FROM python:3.9.6-alpine

# Work directory
WORKDIR /usr/src/web

# Env variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONFAULTHANDLER 1
ENV PYTHONUNBUFFERED 1

# Install psycopg2 dependencies
RUN apk update \
    && apk add postgresql-dev gcc python3-dev musl-dev

# Install dependencies
RUN pip install --upgrade pip
COPY ./requirements.txt .
RUN pip install -r requirements.txt

RUN mkdir logs

ENV FLASK_ENV=development

# Copy project
COPY . .

RUN adduser -S -D -u 1000 docker_user
RUN chown -R 1000:1000 /usr/src/web
USER docker_user
