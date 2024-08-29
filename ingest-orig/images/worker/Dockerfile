FROM python:3

ARG AWS_CONTAINER_CREDENTIALS_RELATIVE_URI

RUN apt-get update && apt-get install -y apt-utils && apt-get -y upgrade
RUN apt-get install -y unixodbc unixodbc-dev odbc-postgresql jq \
                       ca-certificates libxml2 libxml2-dev
RUN apt-get clean

RUN pip3 install --upgrade pip
RUN pip3 install requests pyodbc boto3 awscli bs4 m3u8 lxml

COPY . /usr/src/app
WORKDIR /usr/src/app

RUN chmod ug+x entrypoint.sh
RUN chmod ug+x run.py

ENTRYPOINT ["./entrypoint.sh"]
CMD ["./run.py"]

