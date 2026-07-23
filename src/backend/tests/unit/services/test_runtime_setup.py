from langflow.services.runtime_setup import generate_age_recovery_key


def test_age_recovery_keys_are_one_time_x25519_pairs():
    first_identity, first_recipient = generate_age_recovery_key()
    second_identity, second_recipient = generate_age_recovery_key()

    assert first_identity.startswith("AGE-SECRET-KEY-1")
    assert first_recipient.startswith("age1")
    assert first_identity == first_identity.upper()
    assert first_recipient == first_recipient.lower()
    assert (first_identity, first_recipient) != (second_identity, second_recipient)
