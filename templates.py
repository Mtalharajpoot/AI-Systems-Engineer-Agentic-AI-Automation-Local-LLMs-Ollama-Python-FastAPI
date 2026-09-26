"""Fixed message templates. Safety-critical wording is NEVER written by a model."""


def safety(tid: int) -> str:
    return (f"SAFETY ALERT (ticket #{tid}): if you smell gas, see fire or smoke, or feel unsafe, "
            f"leave the unit now and call your local emergency number. Our on-call manager has "
            f"been alerted and will contact you shortly.")


def water_emergency(tid: int, shutoff: str, ack: float) -> str:
    return (f"We're sorry - we've marked this as an EMERGENCY (ticket #{tid}). If it is safe, shut off the "
            f"main water valve: {shutoff}. Stay away from outlets and light fixtures near the water and "
            f"move valuables out of the way. A plumber has been contacted and our on-call manager has been "
            f"alerted. You will get an update within {ack:g} minutes. If anyone is in danger, call your "
            f"local emergency number.")


def emergency(tid: int, ack: float) -> str:
    return (f"We've marked this as an EMERGENCY (ticket #{tid}). Our on-call manager has been alerted and a "
            f"vendor is being contacted. You will get an update within {ack:g} minutes.")


def urgent(tid: int, ack: float) -> str:
    return (f"Thanks - we've marked this as URGENT (ticket #{tid}) and are contacting a vendor now. "
            f"You will get an update within {ack:g} minutes.")


def routine(tid: int) -> str:
    return (f"Thanks - we've logged this (ticket #{tid}). It is scheduled for the next business day and "
            f"you'll be contacted to arrange access.")


def office(tid: int) -> str:
    return (f"Thanks - this isn't a maintenance request, so we've forwarded it to the office team "
            f"(ticket #{tid}). Expect a reply within 1 business day.")


def received(tid: int) -> str:
    return f"We've received your message (ticket #{tid}) and a member of our team is reviewing it now."


def followup_ack(tid: int) -> str:
    return f"Thanks - we've added this to your open request #{tid}."


def vendor_on_way(vendor: str, eta: int) -> str:
    return f"Update: {vendor} has confirmed and is on the way. Estimated arrival: about {eta} minutes."


def resolved(tid: int) -> str:
    return f"Your request #{tid} has been marked resolved. Reply here if the problem continues."


def pick(urgency: str, category: str, tid: int, prop: dict, ack: float) -> str:
    if urgency == "emergency" and category == "plumbing":
        return water_emergency(tid, prop["water_shutoff"], ack)
    if urgency == "emergency":
        return emergency(tid, ack)
    return urgent(tid, ack)
