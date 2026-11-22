"""Exact-rational linear programming used to bound component concentrations.

The assay lineage keeps every concentration as an exact ``Fraction`` in ppm and
never rounds.  Bounds must range over *all* compatible interpretations, because
a batch may only be signed off as unconditional when no interpretation reaches
the declared limit.  That is a linear programme rather than simple interval
propagation: a blend sample, a phase-separation loss with its own concentration
and a draw split between two destination branches are all linear constraints on
the same specimen contents.

The solver is a two-phase tableau simplex with Bland's rule.  It is exponential
in the worst case but terminating, and a single furnace's ledger is tiny.
"""
from fractions import Fraction

PPM = 10**6


class LPError(ValueError):
    pass


class Infeasible(LPError):
    pass


class Unbounded(LPError):
    pass


class LinearProgram:
    """Maximise ``objective . x`` subject to linear rows over ``x >= 0``.

    Rows carry an insertion-order tag so the caller can ask which sample or
    weighing record supports an extremum.
    """

    def __init__(self, variables):
        self.variables = list(variables)
        self.rows = []

    def add(self, coefficients, relation, rhs, tag=None):
        if relation not in ('<=', '>=', '=='):
            raise LPError(f'unknown relation {relation}')
        coefficients = [Fraction(value) for value in coefficients]
        if len(coefficients) != len(self.variables):
            raise LPError('row width does not match variables')
        rhs = Fraction(rhs)
        if rhs < 0:
            coefficients, rhs = [-value for value in coefficients], -rhs
            relation = {'<=': '>=', '>=': '<=', '==': '=='}[relation]
        self.rows.append((coefficients, relation, rhs, tag or f'row{len(self.rows)}'))
        return len(self.rows) - 1

    def _tableau(self, skip=()):
        n = len(self.variables)
        width = n
        layout = []
        for position, (coefficients, relation, value, tag) in enumerate(self.rows):
            if position in skip:
                continue
            if relation == '<=':
                layout.append((coefficients, relation, value, tag, width, None))
                width += 1
            elif relation == '>=':
                layout.append((coefficients, relation, value, tag, width, width + 1))
                width += 2
            else:
                layout.append((coefficients, relation, value, tag, width, width))
                width += 1
        rows, rhs, basis, artificial = [], [], [], set()
        for coefficients, relation, value, tag, column, art in layout:
            row = list(coefficients) + [Fraction(0)] * (width - n)
            if relation == '<=':
                row[column] = Fraction(1)
                basis.append(column)
            elif relation == '>=':
                row[column], row[art] = Fraction(-1), Fraction(1)
                basis.append(art)
                artificial.add(art)
            else:
                row[art] = Fraction(1)
                basis.append(art)
                artificial.add(art)
            rows.append(row)
            rhs.append(value)
        return rows, rhs, basis, width, artificial

    @staticmethod
    def _objective_row(rows, rhs, basis, width, cost):
        """Build ``z - c.x = value`` and eliminate basic columns from it."""
        objective = [-cost[j] if j < len(cost) else Fraction(0) for j in range(width)]
        value = Fraction(0)
        for index, column in enumerate(basis):
            factor = objective[column]
            if factor == 0:
                continue
            objective = [a - factor * b for a, b in zip(objective, rows[index])]
            value = value - factor * rhs[index]
        return objective, value

    def _run(self, rows, rhs, basis, width, objective, value, forbidden):
        while True:
            basic = set(basis)
            entering = next((j for j in range(width)
                             if objective[j] < 0 and j not in basic and j not in forbidden),
                            None)
            if entering is None:
                return value
            candidates = [(rhs[i] / rows[i][entering], basis[i], i)
                          for i in range(len(rows)) if rows[i][entering] > 0]
            if not candidates:
                raise Unbounded('component bounds are unbounded')
            ratio = min(item[0] for item in candidates)
            # Bland's rule: among the row choices attaining the minimum ratio,
            # leave the one whose basic variable has the smallest index.
            leaving = min((item[1], item[2]) for item in candidates
                          if item[0] == ratio)[1]
            divisor = rows[leaving][entering]
            rows[leaving] = [value / divisor for value in rows[leaving]]
            rhs[leaving] = rhs[leaving] / divisor
            for index in range(len(rows)):
                if index == leaving:
                    continue
                factor = rows[index][entering]
                if factor:
                    rows[index] = [a - factor * b for a, b in zip(rows[index], rows[leaving])]
                    rhs[index] = rhs[index] - factor * rhs[leaving]
            factor = objective[entering]
            objective[:] = [a - factor * b for a, b in zip(objective, rows[leaving])]
            value = value - factor * rhs[leaving]
            basis[leaving] = entering

    def solve(self, objective, skip=()):
        """Return ``(value, solution, tight_row_indices)``; raises on infeasible/unbounded."""
        if len(objective) != len(self.variables):
            raise LPError('objective width does not match variables')
        rows, rhs, basis, width, artificial = self._tableau(skip)
        if not rows:
            return Fraction(0), [Fraction(0)] * len(self.variables), []
        phase_one = [Fraction(-1) if j in artificial else Fraction(0) for j in range(width)]
        obj, value = self._objective_row(rows, rhs, basis, width, phase_one)
        value = self._run(rows, rhs, basis, width, obj, value, set())
        if value != 0:
            raise Infeasible('assay constraints have no compatible interpretation')
        self._drive_out_artificials(rows, rhs, basis, artificial)
        cost = [Fraction(value) for value in objective] + [Fraction(0)] * (width - len(objective))
        for j in artificial:
            cost[j] = Fraction(0)
        obj, value = self._objective_row(rows, rhs, basis, width, cost)
        value = self._run(rows, rhs, basis, width, obj, value, artificial)
        solution = [Fraction(0)] * len(self.variables)
        for column, amount in zip(basis, rhs):
            if column < len(self.variables):
                solution[column] = amount
        tight = []
        for position, (coefficients, relation, bound, tag) in enumerate(self.rows):
            if position in skip:
                continue
            lhs = sum(coef * amount for coef, amount in zip(coefficients, solution))
            if lhs == bound:
                tight.append(position)
        return value, solution, tight

    def _drive_out_artificials(self, rows, rhs, basis, artificial):
        """Make every basic artificial nonbasic so phase two cannot violate ``a = 0``."""
        drop = []
        for index in range(len(rows)):
            if basis[index] not in artificial:
                continue
            entering = next((j for j in range(len(rows[index]))
                             if j not in artificial and rows[index][j] != 0), None)
            if entering is None:
                drop.append(index)          # redundant row: all real coefficients vanish
                continue
            divisor = rows[index][entering]
            rows[index] = [value / divisor for value in rows[index]]
            rhs[index] = rhs[index] / divisor
            for other in range(len(rows)):
                if other == index:
                    continue
                factor = rows[other][entering]
                if factor:
                    rows[other] = [a - factor * b for a, b in zip(rows[other], rows[index])]
                    rhs[other] = rhs[other] - factor * rhs[index]
            basis[index] = entering
        for index in reversed(drop):
            del rows[index], rhs[index], basis[index]


def bounds(program, index):
    """Exact lower/upper bound of one variable plus the tight rows at each extremum."""
    width = len(program.variables)
    high_objective = [Fraction(0)] * width
    high_objective[index] = Fraction(1)
    high, _, high_tight = program.solve(high_objective)
    low_objective = [Fraction(0)] * width
    low_objective[index] = Fraction(-1)
    low_negative, _, low_tight = program.solve(low_objective)
    return -low_negative, high, low_tight, high_tight


def essential_rows(program, objective, tight):
    """Shrink tight rows to an irreducible explanation of an exact extremum.

    A row supports the extremum only if dropping it changes the value; rows that
    merely happen to be tight at one optimal vertex are dropped.  Every
    concentration is purity-bounded, so dropping evidence cannot unbounded the
    programme.
    """
    value, _, _ = program.solve(objective)
    essential = list(tight)
    for position in sorted(tight):
        trial = [row for row in essential if row != position]
        try:
            # Test whether the other rows alone still pin the same extremum.
            # Every non-basic column is fixed there, so a dropped row can only
            # loosen the value, never tighten it.
            if program.solve(objective, skip={row for row in tight
                                               if row not in trial})[0] == value:
                essential = trial
        except (Infeasible, Unbounded):
            continue
    return essential


def explain_bounds(program, index):
    """``(low, high, low_support, high_support)`` with irreducible row explanations."""
    width = len(program.variables)
    high_objective = [Fraction(0)] * width
    high_objective[index] = Fraction(1)
    high, _, high_tight = program.solve(high_objective)
    low_objective = [Fraction(0)] * width
    low_objective[index] = Fraction(-1)
    low_negative, _, low_tight = program.solve(low_objective)
    low = -low_negative
    high_rows = essential_rows(program, high_objective, high_tight)
    low_rows = essential_rows(program, low_objective, low_tight)
    return low, high, low_rows, high_rows
