from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from portfolio.models import Charge, Lease, Property
from portfolio.services import generate_rent, record_payment


class Command(BaseCommand):
    help = "Create synthetic sample data in a separate demo account; never sets a default password."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="demo")

    @transaction.atomic
    def handle(self, *args, **options):
        username = options["username"]
        owner, created = get_user_model().objects.get_or_create(username=username)
        if not created:
            if Property.objects.filter(owner=owner, name__startswith="Демо ·").count() == 3:
                self.stdout.write("Demo data already exists; nothing changed.")
                return
            raise CommandError("Account already exists. Choose a new demo username.")
        owner.set_unusable_password()
        owner.save()
        today = timezone.now().date()
        first = today.replace(day=1)
        for city, currency, name, address, rent in [
            ("moscow", "RUB", "Квартира у парка", "Москва · демонстрационный адрес", "80000"),
            ("minsk", "BYN", "Квартира в центре", "Минск · демонстрационный адрес", "1800"),
            ("dubai", "AED", "Апартаменты Marina", "Дубай · демонстрационный адрес", "9500"),
        ]:
            prop = Property.objects.create(
                owner=owner,
                name="Демо · " + name,
                city=city,
                address=address,
                currency=currency,
                area=65,
                notes="Синтетический пример. Не реальные данные владельца.",
            )
            Lease.objects.create(
                property=prop,
                tenant="Демо-арендатор",
                contact="Контакт не указан",
                start=first,
                end=date(today.year + 1, today.month, 28),
                rent=Decimal(rent),
                deposit=Decimal(rent),
                due_day=5,
            )
        generate_rent(owner, first)
        for prop in owner.property_set.all():
            rent = prop.charges.get(kind="rent")
            amount = rent.amount / 2 if prop.city == "moscow" else rent.amount
            record_payment(owner, rent.pk, amount, today, "Демо · банковский перевод")
            expense = Charge.objects.create(
                property=prop,
                kind="expense",
                title="Демо · коммунальные услуги",
                period=first,
                due=today,
                amount=Decimal("1200") if prop.currency == "RUB" else Decimal("150"),
            )
            record_payment(owner, expense.pk, expense.amount, today, "Демо · оплата поставщику")
            Charge.objects.create(
                property=prop,
                kind="tax",
                title="Демо · налоговое обязательство",
                period=first,
                due=today + timedelta(days=14),
                amount=Decimal("300"),
                notes="Пример суммы, не расчёт налоговой ставки.",
            )
        self.stdout.write(
            self.style.SUCCESS(
                "Synthetic data created. Set a personal password with: python manage.py changepassword "
                + username
            )
        )
