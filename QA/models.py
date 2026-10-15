from django.db import models


class Document(models.Model):
    session_key = models.CharField(max_length=40, db_index=True)
    title = models.CharField(max_length=255)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Chunk(models.Model):
    document = models.ForeignKey(Document, related_name="chunks", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    text = models.TextField()
    embedding = models.JSONField()

    class Meta:
        ordering = ["position"]