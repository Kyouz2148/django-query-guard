from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=120)
    country = models.CharField(max_length=60, default="BR")

    class Meta:
        app_label = "sample_app"

    def __str__(self) -> str:
        return self.name


class Tag(models.Model):
    label = models.CharField(max_length=50, unique=True)

    class Meta:
        app_label = "sample_app"


class Book(models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
    tags = models.ManyToManyField(Tag, related_name="books", blank=True)
    pages = models.IntegerField(default=100)

    class Meta:
        app_label = "sample_app"

    def __str__(self) -> str:
        return self.title
