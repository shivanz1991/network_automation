"""
Network standards constants derived from network_standards/ repository.

This is the single place to update when standards change (new VLANs,
device types, IP offsets, etc.). All other modules import from here.
"""

import ipaddress


REGIONS = {
    "AMER": {"start": 0,   "htcolo": "10.64.0.0/16",  "netinfra": "10.16.0.0/18"},
    "EMEA": {"start": 64,  "htcolo": "10.65.0.0/16",  "netinfra": "10.16.64.0/18"},
    "APAC": {"start": 128, "htcolo": "10.66.0.0/16",  "netinfra": "10.16.128.0/18"},
}

LOCAL_SUPERNETS = {
    "esx_vsan":       {"base": "10.204.0.0/16", "prefix_len": 24},
    "esx_vmkernel":   {"base": "10.200.0.0/16", "prefix_len": 24},
    "ptp":            {"base": "10.205.0.0/16", "prefix_len": 24},
    "tickpublisher":  {"base": "10.10.0.0/16",  "prefix_len": 24},
    "orderentry_nat": {"base": "10.112.0.0/16", "prefix_len": 26},
}

HTCOLO_VLANS = [
    {"offset": 0, "vid": 100, "name": "INFRA",  "prefix_len": 24},
    {"offset": 1, "vid": 110, "name": "MGMT",   "prefix_len": 24},
    {"offset": 2, "vid": 120, "name": "APP",    "prefix_len": 24},
    {"offset": 3, "vid": 130, "name": "ESX",    "prefix_len": 24},
]

SITE_VLANS = [
    (100, "INFRA"), (110, "MGMT"), (120, "APP"), (130, "ESX"),
    (800, "FEED_A"), (801, "FEED_B"),
    (3000, "INFSW_IBGP"), (3050, "TRDSW_INTERLINK"), (3100, "INFRA_AB_INTERLINK"),
]

SITE_GROUPS = ["hub", "spoke"]

DEVICE_CATALOG = {
    "INFSW":      {"role": "INFSW",      "type": "Switch", "offsets": {1: 57, 2: 67, 3: 77, 4: 87}},
    "TRDSW":      {"role": "TRDSW",      "type": "Switch", "offsets": {1: 63, 2: 73, 3: 83, 4: 93}},
    "TIMESERVER": {"role": "TIMESERVER", "type": "Server", "offsets": {1: 30}},
    "PTPSW":      {"role": "PTPSW",      "type": "Switch", "offsets": {1: 32}},
    "CONSOLE":    {"role": "CONSOLE",    "type": "Server", "offsets": {1: 34}},
}

SVI_OFFSETS = {"A": 3, "B": 2, "VRRP": 1}

INTRA_SITE_OFFSET = 4
WAN_INTRA_BASE = ipaddress.IPv4Network("10.0.0.0/21")
REGION_INDEX = {"AMER": 0, "EMEA": 1, "APAC": 2}
ASN_BASE = 65000
WAN_VLAN_BASE = 1000
