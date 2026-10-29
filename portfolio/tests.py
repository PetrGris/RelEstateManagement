import tempfile
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .forms import DocumentForm
from .models import Charge, Document, Lease, Payment, Property
from .services import cancel_charge, deposit_balance, generate_rent, record_payment, void_payment


class PortfolioTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", password="test-only-password")
        self.other = get_user_model().objects.create_user("other", password="test-only-password")
        self.p = Property.objects.create(
            owner=self.owner, name="Квартира", city="moscow", address="Тест", currency="RUB"
        )
        self.lease = Lease.objects.create(
            property=self.p,
            tenant="Тестовый арендатор",
            start=date(2020, 1, 1),
            end=date(2030, 12, 31),
            rent=80000,
            due_day=31,
        )
        self.c = Charge.objects.create(
            property=self.p,
            lease=self.lease,
            kind="rent",
            title="Аренда",
            amount=80000,
            period=date(2020, 2, 1),
            due=date(2020, 2, 29),
        )
        self.client.force_login(self.owner)

    def pay(self, amount, charge=None, token=None):
        return record_payment(
            self.owner, (charge or self.c).pk, Decimal(amount), date(2020, 2, 29), "Тест", token
        )

    def test_partial_and_final_payment(self):
        self.pay("50000")
        self.c.refresh_from_db()
        self.assertEqual(self.c.balance, Decimal("30000"))
        self.assertEqual(self.c.status, "Просрочено")
        self.pay("30000")
        self.assertEqual(self.c.balance, 0)
        self.assertEqual(self.c.status, "Оплачено")

    def test_reject_overpayment_zero_and_future(self):
        for value in ("80000.01", "0", "-1"):
            with self.assertRaises(ValidationError):
                self.pay(value)
        with self.assertRaises(ValidationError):
            record_payment(
                self.owner, self.c.pk, Decimal("1"), self.p.today + timedelta(days=1), ""
            )
        self.assertEqual(Payment.objects.count(), 0)

    def test_payment_submission_idempotent(self):
        token = uuid4()
        first = self.pay("1000", token=token)
        second = self.pay("1000", token=token)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.count(), 1)

    def test_generation_repeatability_and_february(self):
        self.assertEqual(generate_rent(self.owner, date(2024, 2, 1)), 1)
        self.assertEqual(generate_rent(self.owner, date(2024, 2, 1)), 0)
        charge = Charge.objects.get(generated_month=date(2024, 2, 1))
        self.assertEqual(charge.amount, Decimal("80000"))
        self.assertEqual(charge.due, date(2024, 2, 29))
        with self.assertRaises(ValidationError):
            generate_rent(self.owner, date(2024, 2, 2))

    def test_partial_month_and_expired_contract(self):
        self.lease.start, self.lease.end, self.lease.rent = (
            date(2024, 2, 15),
            date(2024, 2, 29),
            Decimal("29000"),
        )
        self.lease.save()
        generate_rent(self.owner, date(2024, 2, 1))
        self.assertEqual(
            Charge.objects.get(generated_month=date(2024, 2, 1)).amount, Decimal("15000")
        )
        self.assertEqual(generate_rent(self.owner, date(2024, 3, 1)), 0)

    def test_contract_validation(self):
        other = Lease(
            property=self.p,
            tenant="Ещё один",
            start=date(2024, 1, 1),
            end=date(2024, 2, 1),
            rent=10,
            due_day=5,
        )
        with self.assertRaises(ValidationError):
            other.full_clean()
        other.start, other.end = date(2032, 2, 1), date(2032, 1, 1)
        with self.assertRaises(ValidationError):
            other.full_clean()

    def test_document_type_and_size(self):
        bad = DocumentForm(files={"file": SimpleUploadedFile("fake.pdf", b"<html>bad</html>")})
        self.assertFalse(bad.is_valid())
        good = DocumentForm(files={"file": SimpleUploadedFile("real.pdf", b"%PDF-1.7 test")})
        self.assertTrue(good.is_valid())

    def test_every_screen_renders(self):
        for url in (
            "/",
            "/properties/",
            f"/properties/{self.p.pk}/",
            "/leases/",
            "/charges/",
            f"/charges/{self.c.pk}/",
            "/reports/",
            "/new/property/",
            "/new/lease/",
            "/new/charge/",
            "/export/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_owner_isolation_and_login(self):
        self.client.force_login(self.other)
        for url in (f"/properties/{self.p.pk}/", f"/charges/{self.c.pk}/"):
            self.assertEqual(self.client.get(url).status_code, 404)
        response = self.client.get("/export/")
        self.assertNotIn("Аренда", response.content.decode("utf-8-sig"))
        self.assertEqual(self.client.get("/").context["property_count"], 0)
        self.assertEqual(
            self.client.post(
                f"/charges/{self.c.pk}/", {"amount": 1, "date": "2020-02-29", "token": uuid4()}
            ).status_code,
            404,
        )
        self.client.logout()
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/login/").status_code, 200)

    def test_cannot_post_another_owners_property(self):
        self.client.force_login(self.other)
        data = {
            "property": self.p.pk,
            "tenant": "Intruder",
            "start": "2032-01-01",
            "end": "2033-01-01",
            "rent": 100,
            "due_day": 1,
            "deposit": 0,
        }
        response = self.client.post("/new/lease/", data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Выберите корректный вариант")
        self.assertEqual(Lease.objects.count(), 1)

    def test_payment_form_end_to_end(self):
        page = self.client.get(f"/charges/{self.c.pk}/")
        token = page.context["form"].initial["token"]
        response = self.client.post(
            f"/charges/{self.c.pk}/",
            {"date": "2020-02-29", "amount": "50000", "reference": "Банк", "token": token},
            follow=True,
        )
        self.assertContains(response, "Оплата записана")
        self.assertEqual(Payment.objects.get().amount, 50000)
        self.assertEqual(self.c.balance, 30000)

    def test_csv_formula_injection(self):
        self.p.name = "=DANGEROUS()"
        self.p.save()
        self.assertIn("'=DANGEROUS()", self.client.get("/export/").content.decode("utf-8-sig"))

    def test_deposits_not_income_and_refund_limit(self):
        dep = Charge.objects.create(
            property=self.p,
            lease=self.lease,
            kind="deposit",
            title="Депозит",
            amount=10000,
            due=date(2020, 1, 1),
            period=date(2020, 1, 1),
        )
        refund = Charge.objects.create(
            property=self.p,
            lease=self.lease,
            kind="deposit_return",
            title="Возврат",
            amount=20000,
            due=date(2020, 1, 1),
            period=date(2020, 1, 1),
        )
        receipt = self.pay("10000", dep)
        self.pay("5000", refund)
        with self.assertRaises(ValidationError):
            self.pay("6000", refund)
        self.assertEqual(deposit_balance(self.lease), 5000)
        with self.assertRaises(ValidationError):
            void_payment(self.owner, receipt.pk, "Ошибка")
        report = self.client.get("/reports/?start=2020-01-01&end=2020-12-31").context["rows"][0]
        self.assertEqual(report["rent"], 0)
        self.assertEqual(report["net"], 0)
        self.assertEqual(report["cash"], 5000)

    def test_void_and_cancel_preserve_history(self):
        payment = self.pay("50000")
        with self.assertRaises(ValidationError):
            cancel_charge(self.owner, self.c.pk, "Ошибка")
        void_payment(self.owner, payment.pk, "Дубликат")
        self.assertEqual(self.c.balance, 80000)
        self.assertEqual(Payment.objects.count(), 1)
        cancel_charge(self.owner, self.c.pk, "Ошибка начисления")
        self.c.refresh_from_db()
        self.assertEqual(self.c.status, "Отменено")
        with self.assertRaises(ValidationError):
            self.pay("1")

    def test_report_cash_date_and_currency(self):
        self.pay("50000")
        other = Property.objects.create(
            owner=self.owner, name="Минск", city="minsk", address="Тест", currency="BYN"
        )
        charge = Charge.objects.create(
            property=other,
            kind="tax",
            title="Налог",
            amount=100,
            due=date(2020, 1, 1),
            period=date(2020, 1, 1),
        )
        self.pay("100", charge)
        report = self.client.get("/reports/?start=2020-02-01&end=2020-02-29").context["rows"]
        self.assertEqual(report[0]["net"], 50000)
        self.assertEqual(report[1]["net"], -100)
        self.assertEqual(report[2]["net"], 0)
        self.assertEqual(
            self.client.get("/reports/?start=2021-01-01&end=2021-01-31").context["rows"][0]["net"],
            0,
        )

    def test_private_document_download(self):
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            response = self.client.post(
                f"/charges/{self.c.pk}/upload/",
                {"file": SimpleUploadedFile("receipt.pdf", b"%PDF-1.7 test")},
            )
            self.assertEqual(response.status_code, 302)
            doc = Document.objects.get()
            response = self.client.get(f"/documents/{doc.pk}/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.7 test")
            self.client.force_login(self.other)
            self.assertEqual(self.client.get(f"/documents/{doc.pk}/").status_code, 404)

    def test_csrf_required(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post("/new/property/", {"name": "x"}).status_code, 403)

    def test_edit_currency_locked_and_other_owner_denied(self):
        response = self.client.post(
            f"/edit/property/{self.p.pk}/",
            {
                "name": "Новое название",
                "city": "moscow",
                "address": "Новый адрес",
                "currency": "AED",
                "area": "50",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.p.refresh_from_db()
        self.assertEqual(self.p.currency, "RUB")
        self.assertEqual(self.p.name, "Новое название")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/edit/property/{self.p.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/edit/lease/{self.lease.pk}/").status_code, 404)

    def test_lease_edit_does_not_change_existing_charges(self):
        response = self.client.post(
            f"/edit/lease/{self.lease.pk}/",
            {
                "property": self.p.pk,
                "tenant": "Арендатор",
                "contact": "",
                "start": "2020-01-01",
                "end": "2030-12-31",
                "rent": "90000",
                "due_day": 5,
                "deposit": 0,
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.lease.refresh_from_db()
        self.c.refresh_from_db()
        self.assertEqual(self.lease.rent, 90000)
        self.assertEqual(self.c.amount, 80000)

    def test_backup_can_restore_database_and_documents(self):
        import sqlite3
        from io import StringIO
        from pathlib import Path
        from zipfile import ZipFile

        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "source.sqlite3"
            media = Path(tmp) / "media"
            media.mkdir()
            (media / "receipt.pdf").write_bytes(b"%PDF-test")
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE proof (amount TEXT)")
                db.execute("INSERT INTO proof VALUES ('50000.00')")
            from copy import deepcopy

            from django.conf import settings

            databases = deepcopy(settings.DATABASES)
            databases["default"]["NAME"] = str(database)
            with override_settings(DATABASES=databases, MEDIA_ROOT=media):
                target = Path(tmp) / "backup.zip"
                call_command("backup", str(target), stdout=StringIO())
            with ZipFile(target) as archive:
                archive.extractall(Path(tmp) / "restored")
            with sqlite3.connect(Path(tmp) / "restored/db.sqlite3") as db:
                self.assertEqual(db.execute("SELECT amount FROM proof").fetchone()[0], "50000.00")
            self.assertEqual((Path(tmp) / "restored/media/receipt.pdf").read_bytes(), b"%PDF-test")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
