from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from preprocess_tex import apply_formula_layout_overrides  # noqa: E402


PARABOLIC_FORMULA = r"""
\begin{equation}
  \left(\sqrt{\mathrm{e}^{y_1}-\theta_1} \xi_1+\frac{\rho_1 \sigma_1}{ \sqrt{\mathrm{e}^{y_1}-\theta_1}} \xi_2\right)^2 + \left(\sqrt{\mathrm{e}^{y_2}-\theta_1} \xi_1+\frac{\rho_2 \sigma_2}{ \sqrt{\mathrm{e}^{y_2}-\theta_1}} \xi_3\right)^2 +\left(\sigma_1^2 \mathrm{e}^{-y_1}-\frac{\rho_1^2 \sigma_1^2}{\mathrm{e}^{y_1}-\theta_1}-2 \theta_1\right) \xi_2^2 + \left(\sigma_2^2 \mathrm{e}^{-y_2}-\frac{\rho_2^2 \sigma_2^2}{\mathrm{e}^{y_2}-\theta_1}-2 \theta_1\right) \xi_3^2.
\end{equation}
"""

NONLINEAR_OPERATOR_FORMULA = r"""
\begin{equation}
\begin{aligned}
\kappa_{\textup{cost}} \sqrt{\frac{2}{\pi\delta t}} \sqrt{\left(\mathrm{e}^{y_1}+\mathrm{e}^{y_2}\right)\left(\frac{\partial^2 I}{\partial x^2}-\frac{\partial I}{\partial x}\right)^2+2\left(\frac{\partial^2 I}{\partial x^2} - \frac{\partial I}{\partial x}\right)\left(\rho_1 \sigma_1 \frac{\partial^2 I}{\partial x \partial y_1}+\rho_2 \sigma_2 \frac{\partial^2 I}{\partial x \partial y_2}\right)+\mathrm{e}^{-y_1}\left(\sigma_1 \frac{\partial^2 I}{\partial x \partial y_1}\right)^2 +\mathrm{e}^{-y_2}\left(\sigma_2 \frac{\partial^2 I}{\partial x \partial y_2}\right)^2 },
\end{aligned}
\end{equation}
"""


class FormulaLayoutTests(unittest.TestCase):
    def test_only_audited_square_completion_gets_continuation_rows(self) -> None:
        transformed, hits = apply_formula_layout_overrides(PARABOLIC_FORMULA)

        self.assertEqual(hits, {"parabolic_square_completion": 1, "nonlinear_operator_radical": 0})
        self.assertEqual(
            transformed.count(r"\\"),
            PARABOLIC_FORMULA.count(r"\\") + 3,
        )
        self.assertIn(r"\begin{aligned}", transformed)
        self.assertEqual(transformed.count("&+"), 3)
        for token in (
            r"\sqrt{\mathrm{e}^{y_1}-\theta_1}",
            r"\sqrt{\mathrm{e}^{y_2}-\theta_1}",
            r"\rho_1^2 \sigma_1^2",
            r"\rho_2^2 \sigma_2^2",
            r"\xi_2^2",
            r"\xi_3^2",
        ):
            self.assertEqual(transformed.count(token), PARABOLIC_FORMULA.count(token))
        self.assertNotIn(r"\fontsize", transformed)
        self.assertNotIn(r"\small", transformed)

    def test_rule_is_idempotent(self) -> None:
        transformed, _ = apply_formula_layout_overrides(PARABOLIC_FORMULA)
        transformed_again, hits = apply_formula_layout_overrides(transformed)
        self.assertEqual(transformed_again, transformed)
        self.assertEqual(hits, {"parabolic_square_completion": 0, "nonlinear_operator_radical": 0})

    def test_nonlinear_operator_radical_gets_nested_continuation_rows(self) -> None:
        transformed, hits = apply_formula_layout_overrides(NONLINEAR_OPERATOR_FORMULA)

        self.assertEqual(hits, {"parabolic_square_completion": 0, "nonlinear_operator_radical": 1})
        self.assertIn(r"\sqrt{\begin{aligned}", transformed)
        self.assertEqual(transformed.count("&+"), 3)
        for token in (
            r"\kappa_{\textup{cost}}",
            r"\sqrt{\frac{2}{\pi\delta t}}",
            r"\mathrm{e}^{-y_1}",
            r"\mathrm{e}^{-y_2}",
            r"\rho_1 \sigma_1",
            r"\rho_2 \sigma_2",
        ):
            self.assertEqual(transformed.count(token), NONLINEAR_OPERATOR_FORMULA.count(token))
        self.assertNotIn(r"\fontsize", transformed)
        self.assertNotIn(r"\small", transformed)

    def test_nonlinear_operator_rule_is_idempotent(self) -> None:
        transformed, _ = apply_formula_layout_overrides(NONLINEAR_OPERATOR_FORMULA)
        transformed_again, hits = apply_formula_layout_overrides(transformed)
        self.assertEqual(transformed_again, transformed)
        self.assertEqual(hits, {"parabolic_square_completion": 0, "nonlinear_operator_radical": 0})

    def test_unrelated_equation_is_unchanged(self) -> None:
        source = r"\begin{equation}x+\int_{\Omega}G\,\mathrm{d}z\end{equation}"
        transformed, hits = apply_formula_layout_overrides(source)

        self.assertEqual(transformed, source)
        self.assertEqual(hits, {"parabolic_square_completion": 0, "nonlinear_operator_radical": 0})


if __name__ == "__main__":
    unittest.main()
