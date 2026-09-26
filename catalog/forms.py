"""Validated private source uploads for catalog administrators."""

import re

from django import forms

from catalog.limits import (
    MAX_CATALOG_BYTES, MAX_EVIDENCE_BYTES, MAX_SUPPLEMENT_MANIFEST_BYTES,
    MAX_SUPPLEMENT_SOURCE_BYTES, MAX_SUPPLEMENT_TOTAL_SOURCE_BYTES,
)


class SourceUploadForm(forms.Form):
    source_kind = forms.ChoiceField(
        label="Тип источника",
        choices=(("catalog", "Конкурсный каталог v4"),
                 ("evidence", "Первичные сведения о моделях")),
    )
    source_file = forms.FileField(label="Исходный файл")
    expected_checksum = forms.CharField(label="Ожидаемый SHA-256", max_length=64)
    rights_basis = forms.CharField(
        label="Основание использования данных", max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    rights_attested = forms.BooleanField(
        label="Подтверждаю право обработки этого источника в закрытом контуре",
    )

    def clean(self):
        cleaned = super().clean()
        checksum = cleaned.get("expected_checksum")
        if checksum and not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
            self.add_error("expected_checksum", "Нужен SHA-256 из 64 шестнадцатеричных знаков.")
        upload = cleaned.get("source_file")
        kind = cleaned.get("source_kind")
        if upload and kind:
            limit = MAX_CATALOG_BYTES if kind == "catalog" else MAX_EVIDENCE_BYTES
            if upload.size == 0 or upload.size > limit:
                self.add_error("source_file", f"Файл должен содержать данные и быть не больше {limit // 1_000_000} МБ.")
            if len(upload.name) > 100:
                self.add_error("source_file", "Имя файла длиннее 100 символов.")
        return cleaned


class MultipleSourceFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleSourceFileField(forms.FileField):
    widget = MultipleSourceFileInput

    def clean(self, data, initial=None):
        if not isinstance(data, (list, tuple)):
            data = [data] if data else []
        if not data:
            raise forms.ValidationError("Передайте первичные файлы производителя.")
        return [forms.FileField.clean(self, item, initial) for item in data]


class SupplementUploadForm(forms.Form):
    manifest_file = forms.FileField(label="Файл дополнения JSON")
    expected_checksum = forms.CharField(label="SHA-256 файла дополнения", max_length=64)
    source_assets = MultipleSourceFileField(label="Первичные HTML/PDF файлы")
    rights_basis = forms.CharField(
        label="Основание обработки первичных сведений", max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    rights_attested = forms.BooleanField(
        label="Подтверждаю право обработки этих файлов в закрытом контуре",
    )

    def clean(self):
        cleaned = super().clean()
        checksum = cleaned.get("expected_checksum")
        if checksum and not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
            self.add_error("expected_checksum", "Нужен SHA-256 из 64 шестнадцатеричных знаков.")
        manifest = cleaned.get("manifest_file")
        if manifest and (manifest.size == 0 or manifest.size > MAX_SUPPLEMENT_MANIFEST_BYTES
                         or len(manifest.name) > 100):
            self.add_error("manifest_file", "Файл JSON пуст, слишком велик или имеет слишком длинное имя.")
        assets = cleaned.get("source_assets") or []
        if len(assets) > 100 or sum(item.size for item in assets) > MAX_SUPPLEMENT_TOTAL_SOURCE_BYTES:
            self.add_error("source_assets", "Превышено допустимое число или общий объём файлов.")
        elif any(item.size == 0 or item.size > MAX_SUPPLEMENT_SOURCE_BYTES
                 for item in assets):
            self.add_error("source_assets", "Первичный файл пуст или превышает допустимый размер.")
        return cleaned
