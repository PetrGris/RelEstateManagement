from pathlib import Path

from django import forms

from .models import Charge, Lease, Payment, Property


class DatesMixin:
    def style(self):
        for field in self.fields.values():
            if isinstance(field, forms.DateField):
                field.widget = forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"})
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs["rows"] = 3


class PropertyForm(forms.ModelForm, DatesMixin):
    class Meta:
        model = Property
        fields = ["name", "city", "address", "currency", "area", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style()


class LeaseForm(forms.ModelForm, DatesMixin):
    class Meta:
        model = Lease
        fields = [
            "property",
            "tenant",
            "contact",
            "start",
            "end",
            "rent",
            "due_day",
            "deposit",
            "notes",
        ]

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["property"].queryset = Property.objects.filter(owner=owner)
        self.style()


class ChargeForm(forms.ModelForm, DatesMixin):
    class Meta:
        model = Charge
        fields = ["property", "lease", "kind", "title", "period", "due", "amount", "notes"]

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["property"].queryset = Property.objects.filter(owner=owner)
        self.fields["lease"].queryset = Lease.objects.filter(property__owner=owner).select_related(
            "property"
        )
        self.style()


class PaymentForm(forms.ModelForm, DatesMixin):
    token = forms.UUIDField(widget=forms.HiddenInput)

    class Meta:
        model = Payment
        fields = ["date", "amount", "reference"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style()


class GenerateForm(forms.Form):
    month = forms.DateField(
        label="Месяц начисления (первое число)", widget=forms.DateInput(attrs={"type": "date"})
    )

    def clean_month(self):
        value = self.cleaned_data["month"]
        if value.day != 1:
            raise forms.ValidationError("Укажите первое число месяца.")
        return value


class DocumentForm(forms.Form):
    file = forms.FileField(label="Квитанция или документ (PDF, JPG, PNG; до 10 МБ)")

    def clean_file(self):
        file = self.cleaned_data["file"]
        if file.size > 10 * 1024 * 1024:
            raise forms.ValidationError("Максимальный размер — 10 МБ.")
        ext = Path(file.name).suffix.lower()
        header = file.read(8)
        file.seek(0)
        valid = {
            ".pdf": header.startswith(b"%PDF-"),
            ".png": header == b"\x89PNG\r\n\x1a\n",
            ".jpg": header.startswith(b"\xff\xd8\xff"),
            ".jpeg": header.startswith(b"\xff\xd8\xff"),
        }
        if not valid.get(ext, False):
            raise forms.ValidationError("Нужен настоящий PDF, JPG или PNG.")
        return file
