# Fonts

**Source Serif 4** by Frank Grießhammer, Adobe — [SIL Open Font License 1.1](LICENSE.md).
Source: <https://github.com/adobe-fonts/source-serif> (release 4.005R).

Self-hosted rather than loaded from a CDN: it keeps the site free of a
third-party dependency, and means no visitor's browsing is reported to Google
Fonts or anyone else.

Subset to Latin plus punctuation, currency and the arrows the interface uses,
with kerning, ligatures, tabular and oldstyle figures retained. That takes the
two weights from 153 KB to 37 KB. To regenerate after a font update:

```bash
pyftsubset SourceSerif4-Regular.ttf.woff2 \
  --output-file=SourceSerif4-Regular.woff2 --flavor=woff2 \
  --unicodes="U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0300-0301,U+0303-0304,U+0308-0309,U+0323,U+0329,U+2000-206F,U+2074,U+20A0-20BF,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD" \
  --layout-features="kern,liga,tnum,onum,frac"
```
