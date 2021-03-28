FROM python:3.9.2-slim

COPY entrypoint.sh /

COPY requirements.txt /
RUN pip3 install -r /requirements.txt

COPY main.py /
ENTRYPOINT ["/entrypoint.sh"]

