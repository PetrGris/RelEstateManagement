import uuid
from builtins import property as computed_property
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

CURRENCIES = [
    ("RUB", "RUB · российский рубль"),
    ("BYN", "BYN · белорусский рубль"),
    ("AED", "AED · дирхам ОАЭ"),
]
CITIES = [("moscow", "Москва"), ("minsk", "Минск"), ("dubai", "Дубай")]
ZONES = {"moscow": "Europe/Moscow", "minsk": "Europe/Minsk", "dubai": "Asia/Dubai"}
positive = MinValueValidator(Decimal("0.01"))


class Property(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name = models.CharField("Название", max_length=150)
    city = models.CharField("Город", max_length=10, choices=CITIES)
    address = models.CharField("Адрес", max_length=300)
    currency = models.CharField("Валюта", max_length=3, choices=CURRENCIES)
    area = models.DecimalField(
        "Площадь, м²", max_digits=9, decimal_places=2, null=True, blank=True, validators=[positive]
    )
    notes = models.TextField("Заметки", blank=True)

    class Meta:
        ordering = ["city", "name"]

    def __str__(self):
        return self.name

    @computed_property
    def today(self):
        return timezone.now().astimezone(ZoneInfo(ZONES[self.city])).date()


class Lease(models.Model):
    property = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name="leases", verbose_name="Квартира"
    )
    tenant = models.CharField("Арендатор", max_length=200)
    contact = models.CharField("Контакт", max_length=200, blank=True)
    start = models.DateField("Начало договора")
    end = models.DateField("Окончание договора")
    rent = models.DecimalField(
        "Аренда в месяц", max_digits=14, decimal_places=2, validators=[positive]
    )
    due_day = models.PositiveSmallIntegerField(
        "День оплаты", default=5, validators=[MinValueValidator(1), MaxValueValidator(31)]
    )
    deposit = models.DecimalField(
        "Согласованный депозит",
        max_digits=14,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    notes = models.TextField("Условия и коммунальные расходы", blank=True)

    def __str__(self):
        return f"{self.property} · {self.tenant}"

    def clean(self):
        if self.start and self.end:
            if self.end < self.start:
                raise ValidationError("Окончание договора раньше начала.")
            if (
                self.property_id
                and Lease.objects.filter(
                    property_id=self.property_id, start__lte=self.end, end__gte=self.start
                )
                .exclude(pk=self.pk)
                .exists()
            ):
                raise ValidationError("На этот период уже есть договор. Сначала уточните его даты.")


class Charge(models.Model):
    KINDS = [
        ("rent", "Аренда"),
        ("reimbursement", "Возмещение коммунальных расходов"),
        ("expense", "Расход владельца"),
        ("tax", "Налог"),
        ("deposit", "Получение депозита"),
        ("deposit_return", "Возврат депозита"),
    ]
    property = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name="charges", verbose_name="Квартира"
    )
    lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, null=True, blank=True, verbose_name="Договор"
    )
    kind = models.CharField("Категория", max_length=20, choices=KINDS)
    title = models.CharField("Назначение", max_length=240)
    period = models.DateField("Дата / начало периода услуги")
    due = models.DateField("Срок оплаты")
    amount = models.DecimalField(
        "Сумма в валюте квартиры", max_digits=14, decimal_places=2, validators=[positive]
    )
    generated_month = models.DateField(null=True, blank=True, editable=False)
    notes = models.TextField("Основание и комментарий", blank=True)
    cancelled_reason = models.CharField(max_length=300, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["due", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["lease", "generated_month"], name="unique_monthly_rent"
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="positive_charge"),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        if self.lease_id and self.property_id and self.lease.property_id != self.property_id:
            raise ValidationError("Договор относится к другой квартире.")
        if (
            self.kind in ("rent", "deposit", "deposit_return", "reimbursement")
            and not self.lease_id
        ):
            raise ValidationError("Выберите договор для расчётов с арендатором.")

    @computed_property
    def incoming(self):
        return self.kind in ("rent", "reimbursement", "deposit")

    @computed_property
    def paid(self):
        return sum((p.amount for p in self.payments.all() if not p.void_reason), Decimal("0"))

    @computed_property
    def balance(self):
        return Decimal("0") if self.cancelled_reason else self.amount - self.paid

    @computed_property
    def status(self):
        if self.cancelled_reason:
            return "Отменено"
        if self.balance <= 0:
            return "Оплачено"
        if self.due < self.property.today:
            return "Просрочено"
        return "Частично оплачено" if self.paid else "К оплате"


class Payment(models.Model):
    charge = models.ForeignKey(Charge, on_delete=models.PROTECT, related_name="payments")
    date = models.DateField("Дата оплаты")
    amount = models.DecimalField("Сумма", max_digits=14, decimal_places=2, validators=[positive])
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    void_reason = models.CharField(max_length=300, blank=True)
    reference = models.CharField("Счёт / способ / номер операции", max_length=240, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-pk"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="positive_payment")
        ]


class Document(models.Model):
    charge = models.ForeignKey(Charge, on_delete=models.PROTECT, related_name="documents")
    file = models.FileField(upload_to="private/%Y/%m/")
    name = models.CharField(max_length=240)
    uploaded = models.DateTimeField(auto_now_add=True)


class Audit(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=300)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created", "-pk"]
