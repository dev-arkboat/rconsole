"""
A simple single-file Flask website.

Run it with:
    pip install flask
    python app.py
Then open http://127.0.0.1:5000 in your browser.
"""

from flask import Flask, render_template_string, request, redirect, url_for

app = Flask(__name__)

# A tiny in-memory "database" of messages (resets when the server restarts).
messages = [
    {"name": "System", "text": "Welcome to the guestbook!"},
]


INDEX_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>My Simple Flask Site</title>
    <style>
        body { font-family: system-ui, sans-serif; max-width: 640px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
        h1 { color: #2c3e50; }
        .msg { border: 1px solid #ddd; border-radius: 8px; padding: 0.5rem 0.75rem; margin: 0.5rem 0; }
        .msg .name { font-weight: bold; }
        .msg .name::after { content: " says:"; font-weight: normal; color: #666; }
        form { display: flex; gap: 0.5rem; margin-top: 1.5rem; }
        input[type=text] { flex: 1; padding: 0.5rem; border: 1px solid #ccc; border-radius: 6px; }
        button { padding: 0.5rem 1rem; border: none; border-radius: 6px; background: #2c3e50; color: #fff; cursor: pointer; }
    </style>
</head>
<body>
    <h1>Hello from Flask 👋</h1>
    <p>This is a tiny single-file website. Leave a message in the guestbook below!</p>

    {% for m in messages %}
        <div class="msg"><span class="name">{{ m.name }}</span> {{ m.text }}</div>
    {% endfor %}

    <form method="post" action="{{ url_for('post_message') }}">
        <input type="text" name="name" placeholder="Your name" required>
        <input type="text" name="text" placeholder="Your message" required>
        <button type="submit">Post</button>
    </form>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_TEMPLATE, messages=messages)


@app.route("/post", methods=["POST"])
def post_message():
    name = request.form.get("name", "Anonymous").strip() or "Anonymous"
    text = request.form.get("text", "").strip()
    if text:
        messages.append({"name": name, "text": text})
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
