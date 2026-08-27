"""HTML resume template — Charter serif, single-column, ATS-friendly, CS new-grad order.

Carried over from v1 (the layout the user approved and locked). Two deliberate changes for v2:
  1. Education shows NO graduation date (user preference).
  2. An optional one-line summary renders under the contact line.

Takes a ``content`` dict (built by renderer.build_content) and returns a full HTML document.
The fit loop searches typography ``scale`` for the largest that still fits one page.
"""
from jinja2 import Template

TEMPLATE = Template(r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{{ name }}</title>
<style>
  @page { size: letter; margin: {{ typ['margin_v'] }}in {{ typ['margin_h'] }}in; }
  * { box-sizing: border-box; }
  @media screen {
    html { background: #e9ecef; padding: 24px 0; }
    body {
      width: 8.5in; min-height: 11in;
      padding: {{ typ['margin_v'] }}in {{ typ['margin_h'] }}in;
      margin: 0 auto; background: white; box-shadow: 0 2px 14px rgba(0,0,0,0.18);
    }
  }
  @media print { html, body { background: white; padding: 0; margin: 0; } }

  html, body {
    font-family: "Charter", "Source Serif Pro", "Source Serif 4", Georgia, "Times New Roman", serif;
    font-size: {{ typ['body_pt'] }}pt; color: #111; line-height: {{ typ['line_height'] }};
  }
  a { color: #111; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .entry-title-link { color: inherit; }

  .header { text-align: center; margin-bottom: {{ typ['header_gap'] }}px; }
  .name { font-size: {{ typ['name_pt'] }}pt; font-weight: 700; letter-spacing: 0.5px; margin: 0 0 2px; }
  .contact { font-size: {{ typ['contact_pt'] }}pt; color: #222; }
  .contact-sep { color: #999; margin: 0 5px; }
  .summary {
    font-size: {{ typ['sub_pt'] }}pt; color: #222; text-align: center;
    margin: 3px auto 0; max-width: 95%;
  }

  h2.section {
    font-size: {{ typ['section_pt'] }}pt; font-weight: 700; text-transform: uppercase;
    letter-spacing: 1.5px; margin: {{ typ['section_top'] }}px 0 3px;
    border-bottom: 0.8pt solid #222; padding-bottom: 1px;
  }
  .entry { margin-bottom: {{ typ['entry_gap'] }}px; }
  .entry-head, .entry-sub {
    display: flex; justify-content: space-between; align-items: baseline; gap: 12px;
  }
  .entry-head { font-size: {{ typ['body_pt'] }}pt; line-height: 1.25; }
  .entry-title { font-weight: 700; }
  .entry-sub { font-size: {{ typ['sub_pt'] }}pt; color: #222; font-style: italic; line-height: 1.25; }
  .entry-right { white-space: nowrap; }

  ul.bullets { margin: 1px 0 0 0; padding-left: 16px; list-style-type: disc; }
  ul.bullets li { margin: {{ typ['bullet_gap'] }}px 0; padding: 0; text-align: justify; }

  .details-line { font-size: {{ typ['sub_pt'] }}pt; margin: 1px 0; }
  .details-label { font-weight: 700; }
  .skills-line { font-size: {{ typ['sub_pt'] }}pt; margin: {{ typ['bullet_gap'] }}px 0; line-height: 1.3; }
  .skills-label { font-weight: 700; }
</style>
</head>
<body>

  <div class="header">
    <div class="name">{{ name }}</div>
    <div class="contact">{{ contact_html | safe }}</div>
  </div>

  {% if education %}
  <h2 class="section">Education</h2>
  {% for ed in education %}
  <div class="entry">
    <div class="entry-head">
      <span class="entry-title">{{ ed.school }}</span>
      <span class="entry-right">{{ ed.location }}</span>
    </div>
    <div class="entry-sub">
      <span>{{ ed.degree_line }}{% if ed.gpa %} &middot; {{ ed.gpa }}{% endif %}</span>
    </div>
    {% for d in ed.details %}<div class="details-line">{{ d | safe }}</div>{% endfor %}
  </div>
  {% endfor %}
  {% endif %}

  {% if experience %}
  <h2 class="section">Experience</h2>
  {% for w in experience %}
  <div class="entry">
    <div class="entry-head">
      <span class="entry-title">{{ w.title }}</span>
      <span class="entry-right">{{ w.dates }}</span>
    </div>
    <div class="entry-sub"><span>{{ w.company }}{% if w.location %} &middot; {{ w.location }}{% endif %}</span></div>
    {% if w.bullets %}<ul class="bullets">{% for b in w.bullets %}<li>{{ b }}</li>{% endfor %}</ul>{% endif %}
  </div>
  {% endfor %}
  {% endif %}

  {% if projects %}
  <h2 class="section">Projects</h2>
  {% for p in projects %}
  <div class="entry">
    <div class="entry-head">
      <span>
        {% if p.link_href %}<a href="{{ p.link_href }}" class="entry-title-link"><span class="entry-title">{{ p.name }}</span></a>{% else %}<span class="entry-title">{{ p.name }}</span>{% endif %}
        {% if p.tech %} <span style="font-style: italic; color: #333;">— {{ p.tech }}</span>{% endif %}
      </span>
      <span class="entry-right">{{ p.dates }}</span>
    </div>
    {% if p.bullets %}<ul class="bullets">{% for b in p.bullets %}<li>{{ b }}</li>{% endfor %}</ul>{% endif %}
  </div>
  {% endfor %}
  {% endif %}

  {% if skills_groups %}
  <h2 class="section">Skills</h2>
  {% for g in skills_groups %}
  <div class="skills-line"><span class="skills-label">{{ g['label'] }}:</span> {{ g['items'] }}</div>
  {% endfor %}
  {% endif %}

</body>
</html>
""")

SCALE_MIN = 0.86
SCALE_MAX = 1.40
SCALE_DEFAULT = 1.0


def typography(scale: float) -> dict:
    scale = max(SCALE_MIN, min(SCALE_MAX, scale))
    return {
        "body_pt": round(11.0 * scale, 2),
        "name_pt": round(22 * scale, 2),
        "section_pt": round(11.0 * scale, 2),
        "sub_pt": round(10.5 * scale, 2),
        "contact_pt": round(10.0 * scale, 2),
        "line_height": round(1.22 + 0.08 * scale, 3),
        "header_gap": round(6 * scale, 1),
        "section_top": round(9 * scale, 1),
        "entry_gap": round(5 * scale, 1),
        "bullet_gap": round(0.8 * scale, 2),
        "margin_v": round(0.4 * (0.9 + 0.1 * scale), 3),
        "margin_h": round(0.5 * (0.92 + 0.08 * scale), 3),
    }


def render_html(content: dict, scale: float = SCALE_DEFAULT) -> str:
    return TEMPLATE.render(typ=typography(scale), **content)
