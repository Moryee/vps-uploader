from app.extensions import db
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import func


class Test(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    datetime = db.Column(db.DateTime(timezone=True), index=True, default=func.now())
    url = db.Column(db.String(256))
    content = db.Column(JSONB)

    def __repr__(self):
        return f'<Test "{self.datetime}">'
