from app.extensions import db
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import validates


class MonopolyMode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    active_tests = db.Column(db.Integer, default=0)
    lock = db.Column(db.Boolean, default=False)

    @validates('active_tests')
    def validate_age(self, key, value):
        if value < 0:
            return 0
        return value

    @classmethod
    def get(cls):
        monopoly = cls.query.filter_by(id=1).first()
        if not monopoly:
            db.session.add(cls(id=1))
            db.session.commit()
            return cls.query.filter_by(id=1).first()
        else:
            return monopoly

    def update_lock(self, lock: bool):
        try:
            db.session.begin_nested()
            self.lock = lock
            db.session.commit()
        except SQLAlchemyError as e:
            db.session.rollback()
            raise e

    def update_active_tests(self, increment: int):
        try:
            db.session.begin_nested()
            self.active_tests += increment
            db.session.commit()
        except SQLAlchemyError as e:
            db.session.rollback()
            raise e

    def _can_start(self, monopoly_mode: bool):
        if monopoly_mode:
            return True if self.active_tests == 0 and not self.lock else False
        else:
            return False if self.lock else True

    def start_mode(self, monopoly_mode: bool) -> bool:
        if monopoly_mode:
            if self._can_start(monopoly_mode):
                self.update_lock(True)
                return True
            return False
        else:
            self.update_active_tests(1)

            if not self._can_start(monopoly_mode):
                self.update_active_tests(-1)
                return False
            return True

    def end_mode(self, monopoly_mode: bool):
        if monopoly_mode and self.lock:
            self.update_lock(False)
        elif not monopoly_mode:
            self.update_active_tests(-1)
        else:
            raise Exception(f'No case found for monopoly_mode: {monopoly_mode} and lock: {self.lock}')
