"""
DemoAirlet core program (spec layout: AirCore/core.py).

The Airlet's logic ships as AirCore assembly text in PROGRAM_ASM; the
loader (airlet.extract_program_asm) reads it with ast.literal_eval —
this file is never executed by the loader. The program is the spec's
own demo: x = 5; if x > 3: y = x + 2, live CHECK_POLICY decisions
("diagnostics" allowed, "network" denied), and the diagnostics()
example from BlackBerry LAN.docx.
"""

PROGRAM_ASM = '''
        LOAD_CONST 5
        STORE_FAST x
        LOAD_FAST x
        LOAD_CONST 3
        COMPARE_GT
        JUMP_IF_FALSE skip
        LOAD_FAST x
        LOAD_CONST 2
        BINARY_ADD
        STORE_FAST y
skip:
        LOAD_CONST "network"
        CHECK_POLICY
        STORE_FAST net_allowed
        LOAD_CONST "diagnostics"
        CHECK_POLICY
        STORE_FAST diag_allowed
        LOAD_NAME diagnostics
        CALL_FUNCTION 0
        STORE_FAST diag
        LOAD_CONST "y"
        LOAD_FAST y
        LOAD_CONST "diag_allowed"
        LOAD_FAST diag_allowed
        LOAD_CONST "net_allowed"
        LOAD_FAST net_allowed
        LOAD_CONST "diag"
        LOAD_FAST diag
        BUILD_MAP 4
        RETURN_VALUE
'''
