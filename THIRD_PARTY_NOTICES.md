# Third-party notices

## Built-in financial-planning skills (`api/coffernest/skills/`)

The playbook text in `api/coffernest/skills/` was written for Coffernest. No text was copied from any external skill. The following projects informed only the structure and conventions:

- **Anthropic Agent Skills / "skill-creator" skill** (Anthropic). Inspired the one-folder-per-skill layout, a `SKILL.md` with YAML frontmatter (`name`, `description`) and a body loaded on demand, and descriptions written as "use when..." triggers.
- **anthropics/financial-services-plugins** (Apache License 2.0). Inspired frontmatter and sectioning conventions for finance-domain skills. Structure only; no content copied.
  License: https://www.apache.org/licenses/LICENSE-2.0
- **curiositech/some_claude_skills** (MIT License). Inspired keeping shared reference material in a separate file that skills point to (our `_shared/` folder, loaded as `india-facts`). Layout idea only; no content copied.

Facts in `api/coffernest/skills/_shared/india-facts-2026-27.md` are cited to their public sources inline.
