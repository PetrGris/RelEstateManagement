import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Audit, Charge, Lease, Payment


@transaction.atomic
def generate_rent(owner, month):
    if month.day != 1:
        raise ValidationError("Выберите первый день месяца.")
    last = date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
    made = 0
    for lease in owner.property_set.all().values_list("leases__pk", flat=True):
        if not lease:
            continue
        obj = Lease.objects.select_for_update().get(pk=lease)
        if obj.start > last or obj.end < month:
            continue
        days = (min(last, obj.end) - max(month, obj.start)).days + 1
        amount = (obj.rent * Decimal(days) / Decimal(last.day)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        _, created = Charge.objects.get_or_create(
            lease=obj,
            generated_month=month,
            defaults={
                "property": obj.property,
                "kind": "rent",
                "title": f"Аренда · {month:%m.%Y} · {obj.tenant}",
                "period": month,
                "due": max(obj.start, date(month.year, month.month, min(obj.due_day, last.day))),
                "amount": amount,
            },
        )
        made += created
    if made:
        Audit.objects.create(
            owner=owner, action=f"Начислена аренда за {month:%m.%Y}: {made} договоров"
        )
    return made


@transaction.atomic
def record_payment(owner, charge_id, amount, day, reference, token=None):
    charge = Charge.objects.select_for_update().get(pk=charge_id, property__owner=owner)
    if token:
        existing = Payment.objects.filter(token=token, charge__property__owner=owner).first()
        if existing:
            if existing.charge_id != charge_id:
                raise ValidationError("Идентификатор уже использован для другого платежа.")
            return existing
    if charge.cancelled_reason:
        raise ValidationError("Обязательство отменено.")
    if charge.lease_id:
        Lease.objects.select_for_update().get(pk=charge.lease_id)
    if charge.kind == "deposit_return" and amount > deposit_balance(charge.lease):
        raise ValidationError("Нельзя вернуть больше полученного остатка депозита.")
    if amount <= 0 or amount > charge.balance:
        raise ValidationError("Сумма должна быть больше нуля и не превышать остаток обязательства.")
    if day > charge.property.today:
        raise ValidationError("Фактическая оплата не может быть датирована будущим числом.")
    kwargs = {"token": token} if token else {}
    payment = Payment(charge=charge, amount=amount, date=day, reference=reference, **kwargs)
    payment.full_clean()
    payment.save()
    Audit.objects.create(
        owner=owner,
        action=f"Оплата #{payment.pk}: {amount} {charge.property.currency}, {charge.title}",
    )
    return payment


def deposit_balance(lease):
    charges = Charge.objects.filter(
        lease=lease, kind__in=["deposit", "deposit_return"]
    ).prefetch_related("payments")
    return sum((c.paid if c.kind == "deposit" else -c.paid for c in charges), Decimal(0))


@transaction.atomic
def void_payment(owner, pk, reason):
    if not reason.strip() or len(reason) > 300:
        raise ValidationError("Укажите причину исправления (до 300 символов).")
    original = Payment.objects.get(pk=pk, charge__property__owner=owner)
    charge = Charge.objects.select_for_update().get(pk=original.charge_id)
    if charge.lease_id:
        Lease.objects.select_for_update().get(pk=charge.lease_id)
    payment = Payment.objects.select_for_update().get(pk=pk)
    if payment.void_reason:
        return
    if charge.kind == "deposit" and deposit_balance(charge.lease) < payment.amount:
        raise ValidationError("Сначала отмените связанные возвраты депозита.")
    payment.void_reason = reason.strip()
    payment.save(update_fields=["void_reason"])
    Audit.objects.create(owner=owner, action=f"Оплата #{pk} отменена: {reason.strip()}")


@transaction.atomic
def cancel_charge(owner, pk, reason):
    if not reason.strip() or len(reason) > 300:
        raise ValidationError("Укажите причину отмены (до 300 символов).")
    charge = Charge.objects.select_for_update().get(pk=pk, property__owner=owner)
    if charge.paid:
        raise ValidationError("Сначала отмените оплаты этого обязательства.")
    if not charge.cancelled_reason:
        charge.cancelled_reason = reason.strip()
        charge.save(update_fields=["cancelled_reason"])
        Audit.objects.create(owner=owner, action=f"Обязательство #{pk} отменено: {reason.strip()}")
