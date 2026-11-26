"""EMS Protocol APP engine for the member site — protocols, the
safety-gate evaluator, and the .acs / .hyperkalemia ePCR generators.

Built 2026-11-26 from Taylor's Drive spec "EMS Protocol APP.docx"
(ACS + Hyperkalemia; safety-rule decision engine; teachbacks in the
steps; smart-phrase ledgers). Same code as the standalone app at
~/workspace/ems-app-build/app/ems_app.py, where its gate battery
passes 12/12. Decision support, not a substitute for local protocol
or medical control.
"""

ACS_STEPS = [
    {"id": "acs-ecg", "level": "BLS/ALS", "title": "12-lead ECG within 10 minutes of first medical contact",
     "detail": "Target diagnostic for every suspected ACS presentation."},
    {"id": "acs-o2", "level": "BLS", "title": "Oxygen only to keep SpO2 >= 94%",
     "detail": "Avoid routine high-flow oxygen unless hypoxic or in respiratory distress."},
    {"id": "acs-asa", "level": "BLS", "title": "Aspirin 162-325 mg chewable, non-enteric coated",
     "detail": "Screen first: active GI bleeding or severe aspirin allergy blocks it (see the safety panel).",
     "gate": "aspirin",
     "teachback": "Explain: this aspirin helps keep platelets from adding to the blockage; ask them to chew it thoroughly for rapid absorption."},
    {"id": "acs-branch", "level": "ALS", "title": "ECG branching: STEMI vs NSTEMI / high-risk",
     "detail": "STEMI: transmit the ECG and activate the STEMI alert / bypass protocol. NSTEMI or high-risk: transport to an appropriate chest pain center."},
    {"id": "acs-iv", "level": "ALS", "title": "Vascular access: 18g or 20g IV, antecubital fossa",
     "detail": "Establish before vasodilator therapy."},
    {"id": "acs-ntg", "level": "ALS", "title": "Nitroglycerin 0.4 mg SL q3-5 min, max 3 doses",
     "detail": "LOCKED until all three prerequisites clear: SBP >= 100 mmHg, no right-ventricular infarction (V4R elevation), no PDE-5 inhibitor use within 24-48 hours.",
     "gate": "nitroglycerin",
     "teachback": "Screen out loud: ask directly about PDE-5 inhibitor use in the last 24-48 hours, and warn the patient the tablet may cause a headache or a warm, flushed feeling."},
    {"id": "acs-analgesia", "level": "ALS", "title": "Analgesia for persistent pain: Fentanyl 25-50 mcg IV/IN or Morphine 2-4 mg IV",
     "detail": "Maintain SBP safety limits; re-check blood pressure after administration.",
     "gate": "analgesia"},
]

HYPERK_STEPS = [
    {"id": "hk-triggers", "level": "BLS/ALS", "title": "Recognize high-risk triggers",
     "detail": "History of ESRD / hemodialysis, missed dialysis sessions, crush injuries, severe burns, or profound generalized weakness."},
    {"id": "hk-ecg", "level": "BLS", "title": "Early 12-lead ECG and rapid transport",
     "detail": "Progression: peaked T waves -> PR prolongation and flattened P waves -> QRS widening / sine wave pattern. Maintain ABCs."},
    {"id": "hk-calcium", "level": "ALS", "title": "Membrane stabilization (severe / symptomatic): Calcium",
     "detail": "Calcium Chloride 10% 1 g IV/IO slow push over 2-5 minutes (preferred in unstable / code scenarios) OR Calcium Gluconate 10% 1-2 g IV/IO slow push over 5-10 minutes.",
     "gate": "calcium",
     "teachback": "Warn the patient they may feel warmth during the calcium push; report burning at the IV site immediately."},
    {"id": "hk-shift", "level": "ALS", "title": "Intracellular shift: Albuterol + Sodium Bicarbonate",
     "detail": "Nebulized Albuterol 10-20 mg continuous. Sodium Bicarbonate 8.4% 50 mEq IV/IO slow push — indicated for severe metabolic acidosis or crush injury.",
     "gate": "shift"},
    {"id": "hk-fluids", "level": "ALS", "title": "Fluid resuscitation: Normal Saline 500-1,000 mL IV/IO bolus",
     "detail": "Only if hypotensive or hypovolemic, and only with no overt pulmonary edema or fluid overload.",
     "gate": "fluids"},
]

PROTOCOLS = {
    "acs": {"name": "Acute Coronary Syndrome (ACS)", "steps": ACS_STEPS},
    "hyperkalemia": {"name": "Hyperkalemia", "steps": HYPERK_STEPS},
}

# ------------------------------------------------------------------
# Safety engine — the spec's prerequisite rules
# ------------------------------------------------------------------

def evaluate(state: dict) -> dict:
    """Return gate results: each restricted action is unlocked only
    when every prerequisite clears; blocking reasons are named."""
    sbp = state.get("sbp")
    gates = {}

    asa_blocks = []
    if state.get("gi_bleed"):
        asa_blocks.append("active GI bleeding")
    if state.get("asa_allergy"):
        asa_blocks.append("severe aspirin allergy")
    gates["aspirin"] = {"unlocked": not asa_blocks, "reasons": asa_blocks}

    ntg_blocks = []
    if sbp is None:
        ntg_blocks.append("SBP not recorded (need >= 100 mmHg)")
    elif sbp < 100:
        ntg_blocks.append(f"SBP {sbp} mmHg < 100 mmHg")
    if state.get("pde5"):
        ntg_blocks.append("PDE-5 inhibitor use within 24-48 hours")
    if state.get("rv_infarct"):
        ntg_blocks.append("right ventricular infarction (V4R elevation)")
    gates["nitroglycerin"] = {"unlocked": not ntg_blocks, "reasons": ntg_blocks}

    anal_blocks = []
    if sbp is None:
        anal_blocks.append("SBP not recorded (maintain SBP safety limits)")
    elif sbp < 100:
        anal_blocks.append(f"SBP {sbp} mmHg < 100 mmHg")
    gates["analgesia"] = {"unlocked": not anal_blocks, "reasons": anal_blocks}

    severe = bool(state.get("ecg_severe"))
    gates["calcium"] = {
        "unlocked": severe,
        "reasons": [] if severe else ["reserved for severe/symptomatic ECG changes (wide QRS / sine wave)"],
    }
    gates["shift"] = {
        "unlocked": severe,
        "reasons": [] if severe else ["intracellular shift follows membrane stabilization in severe cases"],
        "bicarb_note": "indicated" if (state.get("acidosis") or state.get("crush")) else "hold unless severe metabolic acidosis or crush injury",
    }
    fluid_blocks = []
    if not (state.get("hypotensive") or state.get("hypovolemic")):
        fluid_blocks.append("bolus is for hypotension or hypovolemia")
    if state.get("pulmonary_edema"):
        fluid_blocks.append("overt pulmonary edema / fluid overload")
    gates["fluids"] = {"unlocked": not fluid_blocks, "reasons": fluid_blocks}

    return gates

# ------------------------------------------------------------------
# ePCR smart phrases (spec's standard forms)
# ------------------------------------------------------------------

def epcr_acs(f: dict) -> str:
    return (
        f"ACS SUMMARY: Patient presented with {f.get('presentation', '[chest pain/pressure/dyspnea]')} "
        f"onset {f.get('onset_mins', '[ ]')} mins prior to FMC. 12-lead ECG obtained at "
        f"{f.get('ecg_time', '[HH:MM]')} (within 10m) showed {f.get('ecg_finding', '[STEMI/NSTEMI/NSR]')}. "
        f"ECG transmitted: {f.get('transmitted', '[Yes/No]')}. Receiving center notified: "
        f"{f.get('notification', '[STEMI Alert/Standard]')}. Screened negative for PDE-5 inhibitors & V4R RV infarction. "
        f"ASA {f.get('asa_dose', '[162mg/325mg]')} chewed at {f.get('asa_time', '[HH:MM]')}. "
        f"IV {f.get('iv', '[18g/20g]')} AC established. NTG 0.4mg SL x{f.get('ntg_doses', '[ ]')} administered at "
        f"{f.get('ntg_time', '[HH:MM]')} (Pre-BP: {f.get('pre_bp', '[ / ]')}, Post-BP: {f.get('post_bp', '[ / ]')}). "
        f"Pain changed from {f.get('pain_pre', '[ /10]')} to {f.get('pain_post', '[ /10]')}. "
        f"Re-assessed SBP maintained above protocol threshold."
    )


def epcr_hyperkalemia(f: dict) -> str:
    return (
        f"HYPERKALEMIA SUMMARY: High-risk triggers: {f.get('triggers', '[ESRD / Missed Dialysis / Crush Injury / Weakness]')}. "
        f"Last dialysis: {f.get('last_dialysis', '[Date/Time]')}. 12-lead ECG: {f.get('ecg', '[Peaked T / Wide QRS / Sine wave]')}. "
        f"IV/IO {f.get('access', '[18g/20g/IO]')} at {f.get('site', '[Site]')}. "
        f"Calcium {f.get('calcium', '[Chloride 1g / Gluconate 1-2g]')} slow push at {f.get('calcium_time', '[HH:MM]')} "
        f"for membrane stabilization. Nebulized Albuterol {f.get('albuterol', '[10mg/20mg]')} & Sodium Bicarb "
        f"{f.get('bicarb', '[50 mEq IV]')} slow push at {f.get('shift_time', '[HH:MM]')} for intracellular shift. "
        f"NS bolus {f.get('bolus', '[500mL/1000mL]')} IV (lung fields clear). "
        f"Post-tx ECG: {f.get('post_ecg', '[Resolution / Narrowing QRS]')}."
    )
