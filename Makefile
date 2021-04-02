all: build run

build:
	docker-compose build

run:
	docker-compose up

stop:
	docker-compose down

install:
	docker-compose up -d
