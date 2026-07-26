APP_STYLE = r'''
QMainWindow, QWidget#appRoot {
    background: #F7F8FA;
    color: #171A21;
    font-family: "Segoe UI", "Inter", "SF Pro Text", sans-serif;
    font-size: 13px;
}
QWidget { color: #171A21; }
QLabel#brand { font-size: 15px; font-weight: 700; }
QLabel#heroTitle { font-size: 28px; font-weight: 700; }
QLabel#pageTitle { font-size: 22px; font-weight: 700; }
QLabel#sectionTitle { font-size: 15px; font-weight: 650; }
QLabel#muted { color: #707785; }
QLabel#eyebrow { color: #5B5FEF; font-weight: 650; }
QLabel#metricValue { font-size: 14px; font-weight: 650; }
QLabel#statusGood { color: #2E9B55; font-weight: 600; }
QFrame#topbar {
    background: #FFFFFF;
    border-bottom: 1px solid #E7E9EE;
}
QFrame#card, QFrame#roomCard {
    background: #FFFFFF;
    border: 1px solid #E3E6EC;
    border-radius: 12px;
}
QFrame#softCard {
    background: #FAFBFC;
    border: 1px solid #E8EAF0;
    border-radius: 10px;
}
QFrame#videoCard {
    background: #FFFFFF;
    border: 1px solid #E1E4EA;
    border-radius: 12px;
}
QPushButton {
    min-height: 38px;
    padding: 0 16px;
    border-radius: 8px;
    border: 1px solid #D9DDE5;
    background: #FFFFFF;
    color: #20242C;
    font-weight: 600;
}
QPushButton:hover { background: #F6F7F9; border-color: #C9CDD7; }
QPushButton:pressed { background: #EFF1F4; }
QPushButton:disabled { color: #A2A8B3; background: #F5F6F8; border-color: #E4E7EC; }
QPushButton#primary {
    background: #5B5FEF;
    border: 1px solid #5B5FEF;
    color: #FFFFFF;
}
QPushButton#primary:hover { background: #4E52DE; border-color: #4E52DE; }
QPushButton#danger { color: #C54848; background: #FFFFFF; border-color: #E7CACA; }
QPushButton#ghost { background: transparent; border-color: transparent; color: #656C78; padding: 0 8px; }
QPushButton#ghost:hover { background: #F0F2F5; color: #22262E; }
QLineEdit, QSpinBox, QComboBox {
    min-height: 38px;
    padding: 0 11px;
    border-radius: 8px;
    border: 1px solid #D8DCE4;
    background: #FFFFFF;
    selection-background-color: #7477F2;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #666AF0; }
QSlider::groove:horizontal { height: 4px; background: #E3E6EB; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #5B5FEF; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; margin: -5px 0; border-radius: 7px; background: #5B5FEF; }
QToolTip { background: #20242C; color: white; border: 0; padding: 6px; }

#playerControls {
    background: #111318;
    border-top: 1px solid #242832;
    border-bottom-left-radius: 12px;
    border-bottom-right-radius: 12px;
}
QPushButton#playerIconButton {
    background: transparent;
    color: #F5F7FB;
    border: none;
    border-radius: 8px;
    font-size: 18px;
    padding: 0;
}
QPushButton#playerIconButton:hover {
    background: #252936;
}
QPushButton#playerIconButton:disabled {
    color: #6F7582;
    background: transparent;
}
QLabel#playerTime {
    color: #D7DAE2;
    font-size: 12px;
    min-width: 42px;
}
'''
