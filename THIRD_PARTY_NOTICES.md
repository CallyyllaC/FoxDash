# Third-party notices

The Awoo Licence v2.0 applies to FoxDash project material. It does not replace the licences or terms of third-party components.

## Python dependencies

FoxDash declares the following third-party packages in `requirements.txt`:

| Package | Licence |
| --- | --- |
| Textual | MIT |
| Rich | MIT |
| pySerial | BSD 3-Clause |
| BlinkStick | BSD 3-Clause |
| PyUSB | BSD 3-Clause |
| smbus2 | MIT |

The offline calibration tools additionally declare these packages in `tools/calibration/requirements.txt`:

| Package | Licence |
| --- | --- |
| pandas | BSD 3-Clause |
| scikit-learn | BSD 3-Clause |

These packages are not vendored in this repository. The installation scripts obtain them separately from Python package indexes. They may install transitive dependencies under additional licences, and the exact resolved dependency set may vary because the requirement files use version ranges.

If a distribution bundles any dependency, it must retain the copyright, licence, attribution, notice, source-offer, or other materials required by the exact bundled version. The licence files and package metadata distributed with those packages are authoritative for their terms.
