from django import forms

class DXFUploadForm(forms.Form):
    file = forms.FileField()