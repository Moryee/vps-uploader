from app.extensions import db
import time
from flask import current_app


class MonopolyMode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    active_tests = db.Column(db.Integer, default=0)
    lock = db.Column(db.Boolean, default=False)

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
        self.lock = lock
        db.session.commit()

    def update_active_tests(self, increment: int):
        self.active_tests += increment
        db.session.commit()

    def start_mode(self, monopoly_mode: bool):
        if monopoly_mode:
            self.update_lock(True)

            for i in range(20):
                db.session.refresh(self)
                if self.active_tests == 0:
                    break
                current_app.logger.info(f'Waiting for active tests to finish. Active tests: {self.active_tests}')
                time.sleep(10)
            else:
                if self.active_tests > 0:
                    self.update_lock(False)
                    raise Exception('Cannot start monopoly mode while there are active tests')
        else:
            self.update_active_tests(1)

    def end_mode(self, monopoly_mode: bool):
        if monopoly_mode and self.lock:
            self.update_lock(False)
        elif not monopoly_mode:
            self.update_active_tests(-1)
        else:
            raise Exception(f'No case found for monopoly_mode: {monopoly_mode} and lock: {self.lock}')
