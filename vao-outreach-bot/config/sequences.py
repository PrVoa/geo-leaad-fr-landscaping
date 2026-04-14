"""Définition des séquences de messages outreach."""

SEQUENCE = [
    {
        "step": 1,
        "channel": "contact_form",
        "delay_days_after_previous": 0,
        "template": "sequence_1_pain.txt",
        "variants": ["A", "B"],
        "call_after_days": 2,
    },
    {
        "step": 2,
        "channel": "contact_form",
        "delay_days_after_previous": 4,
        "template": "sequence_2_proof.txt",
        "variants": ["A", "B"],
        "call_after_days": 2,
    },
    {
        "step": 3,
        "channel": "contact_form",
        "delay_days_after_previous": 5,
        "template": "sequence_3_urgency.txt",
        "variants": ["A", "B"],
        "call_after_days": 2,
    },
    {
        "step": 4,
        "channel": "email",
        "delay_days_after_previous": 7,
        "template": "sequence_4_email.txt",
        "variants": ["A"],
        "call_after_days": None,
        "email_subject": "devis paysagiste {ville}",
    },
    {
        "step": 5,
        "channel": "email",
        "delay_days_after_previous": 5,
        "template": "sequence_5_breakup.txt",
        "variants": ["A"],
        "call_after_days": None,
        "email_subject": "devis paysagiste {ville}",
    },
]


def get_step(step_number: int) -> dict | None:
    """Retourne la config d'un step par son numéro."""
    for s in SEQUENCE:
        if s["step"] == step_number:
            return s
    return None


def next_step(current: int) -> dict | None:
    """Retourne le step suivant, ou None si séquence terminée."""
    return get_step(current + 1)
