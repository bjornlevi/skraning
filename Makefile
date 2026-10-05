# Makefile for skraning

SHELL  := /bin/bash
PYTHON := .venv/bin/python3

.PHONY: dev install tasks test

dev:
	$(PYTHON) -m flask --app app run --debug --port 5003

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

# Reminders + cleanup. Run from cron every ~10 minutes in production.
tasks:
	$(PYTHON) tasks.py

test:
	$(PYTHON) -m unittest discover -s tests -v
