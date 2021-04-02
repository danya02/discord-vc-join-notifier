FROM python:3.9.2

COPY requirements.txt /
RUN pip3 install --no-cache -r /requirements.txt

COPY names.py /
WORKDIR /
ENTRYPOINT ["python3", "main.py"]
COPY main.py /

