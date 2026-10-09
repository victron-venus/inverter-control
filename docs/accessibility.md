# Accessibility and localization

## Accessible participation and operation

The project accepts issues, discussions, reviews, and documentation contributions as text through GitHub. Documentation uses descriptive headings and links; diagrams have accompanying prose explaining their components and relationships. Reports of inaccessible content are welcome in the issue tracker with the affected page, output, or command and the assistive technology, if relevant.

The daemon has no graphical or web user interface. Its documented commands and text protocols are keyboard-operated; external dashboards are separate projects. Terminal status includes numeric readings, named modes, and explicit `valve=open`, `valve=closed`, or `valve=unknown` text. Color is supplementary rather than the only representation of that valve state.

Set a nonempty `NO_COLOR` environment variable before starting a process to omit the project's ANSI color/style sequences, for example `NO_COLOR=1`. Apply it in the service environment when configuring a managed service; this does not require changing program code. This setting changes presentation only and does not change control or authentication. To inspect behavior without equipment, use the [synthetic quick start](quick-start.md).

The project has not completed a formal WCAG audit or screen-reader acceptance test. Compact, frequently emitted terminal lines may still be difficult to consume with assistive technology. Changes should avoid cursor movement and repeated screen redraws, keep status meaningful without colors, and preserve plain-text alternatives. The repository does not claim conformance for separate dashboards or for GitHub's interface.

## Localization scope and current limitation

Configuration and protocol identifiers, MQTT topics, units, serialized numbers, and timestamps are stable machine-facing contracts and must not be translated. Human-facing logs, diagnostics, help, and status labels are currently English and are not yet extracted into a complete translation catalog. The software is therefore **not currently fully internationalized**; English text is not a reason to mark internationalization not applicable.

For the present small maintainer/operator audience, changes prioritize accurate, searchable diagnostics and compatibility over a partial translation layer that would leave operational errors untranslated. This is an explicit exception to the OpenSSF internationalization recommendation, not a claim that the requirement has been implemented. The [roadmap](../ROADMAP.md) includes evaluating localization with users. A future implementation should extract complete human-facing messages, preserve placeholders and protocol keys, support locale fallback, and test non-ASCII text and translated message expansion before claiming internationalization.
