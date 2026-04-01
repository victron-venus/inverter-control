#!/bin/sh
ssh Cerbo 'svc -t /service/inverter-control'
ssh Cerbo 'svc -t /service/inverter-healthcheck'
