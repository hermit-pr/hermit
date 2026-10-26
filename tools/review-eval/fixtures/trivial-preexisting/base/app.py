"""Tiny demo service."""


def divide(a, b):
    return a / b  # no zero guard: pre-existing bug, out of scope


def greet(name):
    return f"hello {name}"
