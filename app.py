import io
import os
from datetime import date, datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from flask import Flask, Response, redirect, render_template, request, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from functools import wraps

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)

# Auth / session config
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# Database config
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "utility.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            flash("Logged in.", "success")
            return redirect(url_for("home"))
        flash("Wrong password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("home"))

with app.app_context():
    db.create_all()

# ----------------------------
# Models
# ----------------------------
class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(180), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    usage_records = db.relationship("UsageRecord", backref="customer", cascade="all, delete-orphan")
    bills = db.relationship("Bill", backref="customer", cascade="all, delete-orphan")


class UsageRecord(db.Model):
    __tablename__ = "usage_records"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)

    # YYYY-MM (e.g., "2026-01")
    month = db.Column(db.String(7), nullable=False)
    utility_type = db.Column(db.String(32), nullable=False, default="electric")  # electric/water/gas
    usage_value = db.Column(db.Float, nullable=False)  # kWh / gallons / therms

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("customer_id", "month", "utility_type", name="uq_usage_customer_month_type"),
    )


class Bill(db.Model):
    __tablename__ = "bills"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)

    month = db.Column(db.String(7), nullable=False)
    utility_type = db.Column(db.String(32), nullable=False)
    usage_value = db.Column(db.Float, nullable=False)

    rate_per_unit = db.Column(db.Float, nullable=False)
    base_fee = db.Column(db.Float, nullable=False, default=0.0)
    total_amount = db.Column(db.Float, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("customer_id", "month", "utility_type", name="uq_bill_customer_month_type"),
    )


# ----------------------------
# Simple billing engine
# ----------------------------
def calculate_bill(usage_value: float, rate_per_unit: float, base_fee: float) -> float:
    return round(base_fee + (usage_value * rate_per_unit), 2)


DEFAULT_RATES = {
    "electric": {"rate_per_unit": 0.16, "base_fee": 8.00},  # $/kWh, base fee
    "water": {"rate_per_unit": 0.005, "base_fee": 10.00},   # $/gallon (example)
    "gas": {"rate_per_unit": 1.20, "base_fee": 12.00},      # $/therm (example)
}


# ----------------------------
# Routes
# ----------------------------
@app.get("/")
def home():
    customer_count = Customer.query.count()
    usage_count = UsageRecord.query.count()
    bill_count = Bill.query.count()
    return render_template("home.html",
                           customer_count=customer_count,
                           usage_count=usage_count,
                           bill_count=bill_count)


@app.route("/customers", methods=["GET", "POST"])
def customers():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip() or None

        if not full_name:
            flash("Customer name is required.", "danger")
            return redirect(url_for("customers"))

        db.session.add(Customer(full_name=full_name, email=email))
        db.session.commit()
        flash("Customer added.", "success")
        return redirect(url_for("customers"))

    all_customers = Customer.query.order_by(Customer.created_at.desc()).all()
    return render_template("customers.html", customers=all_customers)


@app.get("/customers/<int:customer_id>")
def customer_detail(customer_id: int):
    customer = Customer.query.get_or_404(customer_id)
    usage = (UsageRecord.query
             .filter_by(customer_id=customer_id)
             .order_by(UsageRecord.month.asc(), UsageRecord.utility_type.asc())
             .all())
    bills = (Bill.query
             .filter_by(customer_id=customer_id)
             .order_by(Bill.month.asc(), Bill.utility_type.asc())
             .all())
    return render_template("customer_detail.html", customer=customer, usage=usage, bills=bills)


@app.route("/customers/<int:customer_id>/usage", methods=["POST"])
def add_usage(customer_id: int):
    Customer.query.get_or_404(customer_id)

    month = request.form.get("month", "").strip()          # YYYY-MM
    utility_type = request.form.get("utility_type", "electric").strip()
    usage_value_raw = request.form.get("usage_value", "").strip()

    if len(month) != 7 or month[4] != "-":
        flash("Month must be in YYYY-MM format (example: 2026-01).", "danger")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    try:
        usage_value = float(usage_value_raw)
        if usage_value < 0:
            raise ValueError()
    except ValueError:
        flash("Usage value must be a non-negative number.", "danger")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    if utility_type not in DEFAULT_RATES:
        flash("Invalid utility type.", "danger")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    record = UsageRecord(customer_id=customer_id, month=month, utility_type=utility_type, usage_value=usage_value)

    try:
        db.session.add(record)
        db.session.commit()
        flash("Usage record added.", "success")
    except Exception:
        db.session.rollback()
        flash("That usage record already exists for that month/type (or another DB error occurred).", "danger")

    return redirect(url_for("customer_detail", customer_id=customer_id))


@app.route("/customers/<int:customer_id>/generate_bill", methods=["POST"])
def generate_bill(customer_id: int):
    Customer.query.get_or_404(customer_id)

    month = request.form.get("month", "").strip()
    utility_type = request.form.get("utility_type", "electric").strip()

    record = UsageRecord.query.filter_by(customer_id=customer_id, month=month, utility_type=utility_type).first()
    if not record:
        flash("No usage record found for that month/type.", "danger")
        return redirect(url_for("customer_detail", customer_id=customer_id))

    rate_info = DEFAULT_RATES[utility_type]
    total = calculate_bill(record.usage_value, rate_info["rate_per_unit"], rate_info["base_fee"])

    bill = Bill(
        customer_id=customer_id,
        month=month,
        utility_type=utility_type,
        usage_value=record.usage_value,
        rate_per_unit=rate_info["rate_per_unit"],
        base_fee=rate_info["base_fee"],
        total_amount=total,
    )

    try:
        db.session.add(bill)
        db.session.commit()
        flash("Bill generated.", "success")
    except Exception:
        db.session.rollback()
        flash("Bill already exists for that month/type (or another DB error occurred).", "danger")

    return redirect(url_for("customer_detail", customer_id=customer_id))


@app.get("/customers/<int:customer_id>/usage_chart.png")
def usage_chart(customer_id: int):
    customer = Customer.query.get_or_404(customer_id)

    # For demo: chart electric usage over time
    records = (UsageRecord.query
               .filter_by(customer_id=customer_id, utility_type="electric")
               .order_by(UsageRecord.month.asc())
               .all())

    months = [r.month for r in records]
    values = [r.usage_value for r in records]

    fig, ax = plt.subplots()
    ax.set_title(f"Electric Usage Over Time — {customer.full_name}")
    ax.set_xlabel("Month")
    ax.set_ylabel("Usage (kWh)")
    if months:
        ax.plot(months, values, marker="o")
        ax.tick_params(axis="x", rotation=45)
    else:
        ax.text(0.5, 0.5, "No electric usage records yet", ha="center", va="center", transform=ax.transAxes)

    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)

    return Response(buf.getvalue(), mimetype="image/png")


# ----------------------------
# CLI helper
# ----------------------------
@app.cli.command("init-db")
def init_db():
    """Initialize the database tables."""
    db.create_all()
    print("Database initialized.")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)