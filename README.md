# SFPS: Serverless File Processing System case study

An event-driven AWS file-processing pipeline built with Amazon S3, AWS Lambda, Amazon DynamoDB,
Amazon SQS, Amazon CloudWatch, AWS IAM, and Python 3.13.

This repository hosts the full engineering case study: architecture, implementation, packaging,
security, the test matrix, the captured SQS failure envelope, and the evidence gaps.

## Publish it on GitHub Pages

1. Create a new public repository, for example `sfps-case-study`.
2. Push the contents of this folder to the `main` branch.
3. In the repository, open **Settings → Pages**.
4. Under **Build and deployment**, choose **Deploy from a branch**.
5. Select the `main` branch and the `/ (root)` folder, then save.
6. Wait a minute. The case study will be live at:

   `https://<your-username>.github.io/sfps-case-study/`

That URL is the link for your LinkedIn post and first comment. `index.html` opens the HTML edition.
The PDF and DOCX are downloadable from the same page.

## What is in this folder

| File | Purpose |
|---|---|
| `index.html` | The full self-contained HTML case study. GitHub Pages serves this as the home page. |
| `SFPS_Complete_Project_Case_Study.pdf` | PDF export of the Word edition, 47 pages. |
| `SFPS_Complete_Project_Case_Study.docx` | Editable Word edition. |
| `SFPS_Evidence_Inventory.html` | The evidence inventory: sources, screenshots, omissions, unverified claims, outstanding tests. |
| `SFPS_LinkedIn_Carousel.pdf` | The 10-slide LinkedIn carousel. |
| `.nojekyll` | Tells GitHub Pages to serve the files as-is. |

The diagrams are mine, drawn from the implemented code and the deployed configuration. The console
captures are unchanged apart from a hairline border and, in a few cases, a crop of the browser top
bar.

## Notes

- No access keys, secret keys, session tokens, or passwords appear in the case study or the captures.
- The published build replaces the AWS account number with `000000000000` in every screenshot where
  it appeared: 13 locations across 9 figures.
- Some behavior is verified by tests or the console captures. Some is documented but not proven.
  The evidence inventory says which is which.
