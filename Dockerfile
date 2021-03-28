FROM python:3.9.2-slim

COPY requirements.txt /
RUN pip3 install -r /requirements.txt

COPY main.py /
WORKDIR /
ENTRYPOINT ["python3", "main.py"]

