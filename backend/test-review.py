"""Sample payment utility to test AI PR Review Agent."""
import hashlib
import random

# Intentional security issue: hardcoded secret & weak hash
AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"

def create_idempotency_key(order_id: str) -> str:
    # Intentional quality/test issue: lacks type checks & tests
    noise = random.random()
    return hashlib.md5(f"{order_id}{noise}".encode()).hexdigest()

def process_charge(customer_id, amount_cents):
    # Intentional docs issue: missing docstring, missing type hints
    if amount_cents <= 0:
        raise ValueError("Invalid charge")
    return {"status": "success", "id": customer_id}