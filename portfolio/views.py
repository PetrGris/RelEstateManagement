import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import ChargeForm, DocumentForm, GenerateForm, LeaseForm, PaymentForm, PropertyForm
from .models import CITIES, CURRENCIES, Audit, Charge, Document, Lease, Payment, Property
from .services import cancel_charge, generate_rent, record_payment, void_payment


def charges_for(user):
    return (
        Charge.objects.filter(property__owner=user)
        .select_related("property", "lease")
        .prefetch_related("payments", "documents")
    )


def selected_charges(request):
    qs = charges_for(request.user)
    if request.GET.get("city") in dict(CITIES):
        qs = qs.filter(property__city=request.GET["city"])
    if request.GET.get("kind") in dict(Charge.KINDS):
        qs = qs.filter(kind=request.GET["kind"])
    if request.GET.get("property", "").isdigit():
        qs = qs.filter(property_id=request.GET["property"])
    rows = list(qs)
    state = request.GET.get("state")
    if state == "open":
        rows = [c for c in rows if c.balance > 0]
    elif state == "overdue":
        rows = [c for c in rows if c.balance > 0 and c.due < c.property.today]
    elif state == "paid":
        rows = [c for c in rows if c.status == "Оплачено"]
    return rows


@login_required
def dashboard(request):
    rows = list(charges_for(request.user).filter(cancelled_reason=""))
    cards = []
    for currency, _ in CURRENCIES:
        items = [c for c in rows if c.property.currency == currency]
        received = sum((c.paid for c in items if c.incoming and c.kind != "deposit"), Decimal(0))
        spent = sum(
            (c.paid for c in items if not c.incoming and c.kind != "deposit_return"), Decimal(0)
        )
        debt = sum((c.balance for c in items if c.incoming and c.kind != "deposit"), Decimal(0))
        deposit = sum(
            (
                c.paid if c.kind == "deposit" else -c.paid
                for c in items
                if c.kind in ("deposit", "deposit_return")
            ),
            Decimal(0),
        )
        cards.append(
            {
                "currency": currency,
                "received": received,
                "spent": spent,
                "net": received - spent,
                "debt": debt,
                "deposit": deposit,
            }
        )
    pending = sorted([c for c in rows if c.balance > 0], key=lambda c: c.due)
    return render(
        request,
        "portfolio/dashboard.html",
        {
            "page": "overview",
            "cards": cards,
            "pending": pending[:8],
            "property_count": Property.objects.filter(owner=request.user).count(),
            "overdue_count": sum(c.due < c.property.today for c in pending),
            "audit": Audit.objects.filter(owner=request.user)[:5],
        },
    )


@login_required
def properties(request):
    return render(
        request,
        "portfolio/properties.html",
        {
            "page": "properties",
            "properties": Property.objects.filter(owner=request.user).prefetch_related("leases"),
        },
    )


@login_required
def property_detail(request, pk):
    obj = get_object_or_404(Property, pk=pk, owner=request.user)
    return render(
        request,
        "portfolio/property.html",
        {
            "page": "properties",
            "property": obj,
            "leases": obj.leases.all(),
            "charges": charges_for(request.user).filter(property=obj),
        },
    )


@login_required
def create(request, kind):
    definitions = {
        "property": (PropertyForm, "Новая квартира", "properties"),
        "lease": (LeaseForm, "Новый договор", "leases"),
        "charge": (ChargeForm, "Новое обязательство", "charges"),
    }
    if kind not in definitions:
        from django.http import Http404

        raise Http404
    cls, title, destination = definitions[kind]
    kwargs = {} if kind == "property" else {"owner": request.user}
    initial = {}
    if (
        request.GET.get("property", "").isdigit()
        and Property.objects.filter(pk=request.GET["property"], owner=request.user).exists()
    ):
        initial["property"] = request.GET["property"]
    form = cls(request.POST or None, initial=initial, **kwargs)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            obj = form.save(commit=False)
            if kind == "property":
                obj.owner = request.user
            obj.save()
            Audit.objects.create(owner=request.user, action=f"{title}: {obj}")
        messages.success(request, "Запись сохранена.")
        return redirect(destination)
    return render(
        request,
        "portfolio/form.html",
        {
            "title": title,
            "form": form,
            "hint": "Суммы указываются в валюте выбранной квартиры. Депозит регистрируется отдельным обязательством и не входит в арендный доход.",
        },
    )


@login_required
def leases(request):
    form = GenerateForm(
        request.POST or None, initial={"month": timezone.now().date().replace(day=1)}
    )
    if request.method == "POST" and form.is_valid():
        count = generate_rent(request.user, form.cleaned_data["month"])
        messages.success(
            request, f"Создано начислений: {count}. Повторное начисление не создаёт дубликатов."
        )
        return redirect("charges")
    return render(
        request,
        "portfolio/leases.html",
        {
            "page": "leases",
            "leases": Lease.objects.filter(property__owner=request.user).select_related("property"),
            "form": form,
        },
    )


@login_required
def charges(request):
    return render(
        request,
        "portfolio/charges.html",
        {
            "page": "charges",
            "charges": selected_charges(request),
            "cities": CITIES,
            "kinds": Charge.KINDS,
            "properties": Property.objects.filter(owner=request.user),
        },
    )


@login_required
def charge_detail(request, pk):
    obj = get_object_or_404(charges_for(request.user), pk=pk)
    form = PaymentForm(
        request.POST or None,
        initial={"date": obj.property.today, "amount": obj.balance, "token": uuid4()},
    )
    if request.method == "POST" and form.is_valid():
        try:
            record_payment(
                request.user,
                obj.pk,
                form.cleaned_data["amount"],
                form.cleaned_data["date"],
                form.cleaned_data["reference"],
                form.cleaned_data["token"],
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Оплата записана, остаток обновлён.")
            return redirect("charge", pk=pk)
    return render(
        request,
        "portfolio/charge.html",
        {"page": "charges", "charge": obj, "form": form, "document_form": DocumentForm()},
    )


@login_required
@require_POST
def upload(request, pk):
    obj = get_object_or_404(Charge, pk=pk, property__owner=request.user)
    form = DocumentForm(request.POST, request.FILES)
    if form.is_valid():
        file = form.cleaned_data["file"]
        name = Path(file.name).name[:240]
        file.name = uuid4().hex + Path(name).suffix.lower()
        Document.objects.create(charge=obj, file=file, name=name)
        Audit.objects.create(
            owner=request.user, action=f"Документ добавлен к обязательству #{obj.pk}"
        )
        messages.success(request, "Документ сохранён.")
    else:
        messages.error(request, " ".join(str(e) for errors in form.errors.values() for e in errors))
    return redirect("charge", pk=pk)


@login_required
def document(request, pk):
    obj = get_object_or_404(Document, pk=pk, charge__property__owner=request.user)
    response = FileResponse(obj.file.open("rb"), as_attachment=True, filename=obj.name)
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def export(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="estate-ledger.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(
        [
            "Квартира",
            "Город",
            "Категория",
            "Назначение",
            "Период",
            "Срок",
            "Валюта",
            "Начислено",
            "Оплачено",
            "Остаток",
            "Статус",
        ]
    )

    def safe(value):
        value = str(value)
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value

    for c in selected_charges(request):
        writer.writerow(
            [
                safe(c.property.name),
                c.property.get_city_display(),
                c.get_kind_display(),
                safe(c.title),
                c.period,
                c.due,
                c.property.currency,
                c.amount,
                c.paid,
                c.balance,
                c.status,
            ]
        )
    return response


@login_required
def reports(request):
    today = timezone.now().date()
    try:
        start = date.fromisoformat(request.GET.get("start", str(today.replace(day=1))))
        end = date.fromisoformat(request.GET.get("end", str(today)))
        if start > end:
            raise ValueError
    except ValueError:
        messages.error(request, "Неверный период: укажите даты от начала до окончания.")
        start, end = today.replace(day=1), today
    result = []
    for currency, _ in CURRENCIES:
        rows = list(charges_for(request.user).filter(property__currency=currency))

        def paid(kinds, rows=rows):
            return sum(
                (
                    p.amount
                    for c in rows
                    if c.kind in kinds
                    for p in c.payments.all()
                    if not p.void_reason and start <= p.date <= end
                ),
                Decimal(0),
            )

        rent = paid(["rent"])
        reimb = paid(["reimbursement"])
        expenses = paid(["expense"])
        tax = paid(["tax"])
        deposit_in, deposit_out = paid(["deposit"]), paid(["deposit_return"])
        accrued = sum(
            (
                c.amount
                for c in rows
                if c.kind == "rent" and not c.cancelled_reason and start <= c.period <= end
            ),
            Decimal(0),
        )
        result.append(
            {
                "currency": currency,
                "rent": rent,
                "reimb": reimb,
                "expenses": expenses,
                "tax": tax,
                "deposit_in": deposit_in,
                "deposit_out": deposit_out,
                "net": rent + reimb - expenses - tax,
                "cash": rent + reimb + deposit_in - expenses - tax - deposit_out,
                "accrued": accrued,
            }
        )
    return render(
        request,
        "portfolio/reports.html",
        {"page": "reports", "rows": result, "start": start.isoformat(), "end": end.isoformat()},
    )


@login_required
@require_POST
def correction(request, kind, pk):
    if kind == "payment":
        obj = get_object_or_404(Payment, pk=pk, charge__property__owner=request.user)
        charge_id = obj.charge_id
        action = void_payment
    elif kind == "charge":
        obj = get_object_or_404(Charge, pk=pk, property__owner=request.user)
        charge_id = obj.pk
        action = cancel_charge
    else:
        from django.http import Http404

        raise Http404
    try:
        action(request.user, pk, request.POST.get("reason", ""))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Отмена записана. История сохранена.")
    return redirect("charge", pk=charge_id)


@login_required
def edit(request, kind, pk):
    if kind == "property":
        obj = get_object_or_404(Property, pk=pk, owner=request.user)
        form = PropertyForm(request.POST or None, instance=obj)
        if obj.charges.exists() or obj.leases.exists():
            form.fields["currency"].disabled = True
        title = "Изменить квартиру"
        destination = "property"
    elif kind == "lease":
        obj = get_object_or_404(Lease, pk=pk, property__owner=request.user)
        form = LeaseForm(request.POST or None, instance=obj, owner=request.user)
        if obj.charge_set.exists():
            form.fields["property"].disabled = True
        title = "Изменить договор"
        destination = "leases"
    else:
        from django.http import Http404

        raise Http404
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            previous = type(obj).objects.get(pk=obj.pk)
            changes = "; ".join(
                f"{field}: {getattr(previous, field)} → {form.cleaned_data[field]}"
                for field in form.changed_data
            )
            form.save()
            if changes:
                Audit.objects.create(
                    owner=request.user, action=f"{title} #{obj.pk}: {changes}"[:300]
                )
        messages.success(
            request,
            "Изменения сохранены. Уже созданные финансовые обязательства сохранены без изменений.",
        )
        return redirect(destination, **({"pk": obj.pk} if destination == "property" else {}))
    return render(
        request,
        "portfolio/form.html",
        {
            "title": title,
            "form": form,
            "hint": "Изменение ставки и дат договора влияет на будущие начисления. Существующие суммы сохраняются; для исправления отмените ошибочное обязательство с указанием причины и создайте новое. Валюта квартиры с договорами и расчётами защищена от изменения.",
        },
    )
