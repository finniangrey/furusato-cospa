/**
 * ふるさと納税の控除上限額（自己負担 2,000 円で済む寄付額）の簡易計算。
 * 給与所得者向けの目安であり、住宅ローン控除・医療費控除・iDeCo などは考慮しない。
 *
 *   上限額 = 住民税所得割額 × 20% ÷ (100% − 住民税率10% − 所得税率 × 1.021) + 2,000円
 */

export interface SimulatorInput {
  /** 給与収入（円・額面） */
  income: number;
  /** 配偶者控除の対象となる配偶者がいるか */
  spouse: boolean;
  /** 16〜18歳の扶養親族の人数（一般の扶養控除） */
  dependentsGeneral: number;
  /** 19〜22歳の扶養親族の人数（特定扶養控除） */
  dependentsSpecific: number;
}

/** 給与所得控除（令和7年分以降） */
function salaryDeduction(income: number): number {
  if (income <= 1_900_000) return 650_000;
  if (income <= 3_600_000) return income * 0.3 + 80_000;
  if (income <= 6_600_000) return income * 0.2 + 440_000;
  if (income <= 8_500_000) return income * 0.1 + 1_100_000;
  return 1_950_000;
}

/** 所得税の基礎控除（令和7・8年分の上乗せ特例を含む。合計所得 2,350 万円超の逓減は扱わない） */
function basicDeductionIncomeTax(totalIncome: number): number {
  if (totalIncome <= 1_320_000) return 950_000;
  if (totalIncome <= 3_360_000) return 880_000;
  if (totalIncome <= 4_890_000) return 680_000;
  if (totalIncome <= 6_550_000) return 630_000;
  return 580_000;
}

function incomeTaxRate(taxable: number): number {
  if (taxable <= 1_950_000) return 0.05;
  if (taxable <= 3_300_000) return 0.1;
  if (taxable <= 6_950_000) return 0.2;
  if (taxable <= 9_000_000) return 0.23;
  if (taxable <= 18_000_000) return 0.33;
  if (taxable <= 40_000_000) return 0.4;
  return 0.45;
}

/** 控除上限額の目安（円・1,000円未満切り捨て）。計算できない場合は 0 */
export function estimateLimit(input: SimulatorInput): number {
  const { income, spouse, dependentsGeneral, dependentsSpecific } = input;
  if (!(income > 0)) return 0;

  const earned = Math.max(0, income - salaryDeduction(income));
  const socialInsurance = income * 0.15; // 社会保険料は年収の 15% と仮定

  const taxableIncomeTax = Math.max(
    0,
    earned -
      socialInsurance -
      basicDeductionIncomeTax(earned) -
      (spouse ? 380_000 : 0) -
      dependentsGeneral * 380_000 -
      dependentsSpecific * 630_000,
  );
  const taxableResident = Math.max(
    0,
    earned -
      socialInsurance -
      430_000 -
      (spouse ? 330_000 : 0) -
      dependentsGeneral * 330_000 -
      dependentsSpecific * 450_000,
  );

  // 所得割額（税率 10%）。調整控除は一律 2,500 円として概算する
  const residentTax = Math.max(0, taxableResident * 0.1 - 2_500);
  if (residentTax <= 0) return 0;

  const limit = (residentTax * 0.2) / (0.9 - incomeTaxRate(taxableIncomeTax) * 1.021) + 2_000;
  return Math.floor(limit / 1000) * 1000;
}
