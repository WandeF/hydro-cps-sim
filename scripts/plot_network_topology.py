#!/usr/bin/env python3
"""Draw config.yaml topology with supplied vector device icons and PLC mapping boxes.

Requires PyYAML, CairoSVG, and Pillow. Visible text is a device name, IP, or attack number.
The standalone SVG embeds vector paths; PDF is vector; PNG is 600 dpi.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from io import BytesIO
import ipaddress
import json
import math
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

import cairosvg
from PIL import Image
import yaml

ROOT = Path(__file__).resolve().parents[1]
NAME = 'ctown_network_topology'
NS = 'http://www.w3.org/2000/svg'
ET.register_namespace('', NS)
INK = '#292D32'
LINE = '#687789'


def tag(name):
    return f'{{{NS}}}{name}'


def element(parent, name, **attrs):
    return ET.SubElement(parent, tag(name), {k.replace('_', '-'): str(v) for k, v in attrs.items()})


def read_topology(path):
    config = yaml.safe_load(path.read_text())
    net = config["network"]
    nodes = {n["name"]: n for group in net["nodes"].values() for n in group}
    core, = [n["name"] for n in net["nodes"]["routers"] if n["role"] == "core_router"]
    plcs = {p["name"]: p for p in config["plcs"]}
    branches = {}
    edges = []
    for lan in net["lans"]:
        router, = [n for n in lan["members"] if nodes[n]["role"] == "edge_router"]
        switch, = [n for n in lan["members"] if nodes[n]["role"] == "bridge_switch"]
        endpoint, = [n for n in lan["members"] if nodes[n]["role"] in ("plc", "scada")]
        link, = [l for l in net["backbone_links"] if set(l["endpoints"]) == {core, router}]
        branches[endpoint] = dict(router=router, switch=switch, lan=lan, link=link)
        edges.extend([(core, router), (router, switch), (switch, endpoint)])
    assert set(branches) == set(plcs) | {config["scada"]["name"]}
    assert set(nodes) == {core} | {v for edge in edges for v in edge}
    assert len(edges) == len(nodes) - 1
    assert {p["name"] for p in net["attack_points"]} == {"core_backbone"} | {
        f"{name.lower()}_path" for name in branches
    }
    for point in net["attack_points"]:
        if point["type"] == "link":
            assert set(point["targets"]) == {b["link"]["name"] for b in branches.values()}
        else:
            endpoint = point["targets"][0]
            b = branches[endpoint]
            assert point["targets"] == [endpoint, b["switch"], b["router"], core]
    apps = net["applications"]
    assert {(a["src"], a["dst"], a["protocol"], a["dst_port"]) for a in apps} == {
        (config["scada"]["name"], p, "tcp", 502) for p in plcs
    }
    # This publication layout intentionally fails for a different-sized topology.
    assert set(plcs) == {f"PLC{i}" for i in (1, 2, 3, 4, 5, 7, 8, 9)}
    return config, branches, edges


class Diagram:
    width = 1840
    height = 1130

    def __init__(self, asset_dir):
        self.root = ET.Element(tag('svg'), {'viewBox': f'0 0 {self.width} {self.height}',
            'width': '210mm', 'height': f'{210*self.height/self.width:.6f}mm'})
        element(self.root, 'rect', x=0, y=0, width=self.width, height=self.height, fill='white')
        self.background = element(self.root, 'g', id='group-boxes')
        self.wires = element(self.root, 'g', id='network-links', fill='none', stroke=LINE,
                             stroke_width=2.2, stroke_linejoin='round')
        self.devices = element(self.root, 'g', id='device-icons')
        self.labels = element(self.root, 'g', id='device-names-and-addresses',
            font_family='DejaVu Sans, sans-serif', fill=INK, text_anchor='middle')
        self.icons = {}
        self.mapping = {}
        self.node_names = []
        self.device_positions = {}
        for kind in ('router', 'switch', 'plc', 'pump', 'valve', 'tank'):
            icon = ET.parse(asset_dir / f'{kind}.svg').getroot()
            # The supplied switch paths have fill="". Normalize this invalid
            # empty value in the embedded copy so every renderer shows them.
            for node in icon.iter():
                if node.get('fill') == '':
                    node.set('fill', '#2c2c2c')
            for child in list(icon):
                if child.tag == tag('metadata'):
                    icon.remove(child)
            # Find transparent source margins, then adjust the vector viewBox.
            # This raster is used only to measure bounds; it is never embedded.
            image = Image.open(BytesIO(cairosvg.svg2png(bytestring=ET.tostring(icon),
                                                       output_width=1024, output_height=1024)))
            bbox = image.getbbox()
            if bbox is None:
                raise ValueError(f'Invisible icon: {kind}')
            vx, vy, vw, vh = map(float, icon.attrib['viewBox'].split())
            left, top, right, bottom = bbox
            pad = 8
            icon.set('viewBox', f'{vx+(left-pad)*vw/1024} {vy+(top-pad)*vh/1024} '
                     f'{(right-left+2*pad)*vw/1024} {(bottom-top+2*pad)*vh/1024}')
            self.icons[kind] = icon

    def text(self, x, y, value, size=22, bold=False):
        if '/' in value:
            width = len(value)*size*0.64
            element(self.labels, 'rect', x=x-width/2, y=y-size, width=width,
                    height=size*1.2, fill='white')
        node = element(self.labels, 'text', x=x, y=y, font_size=size,
                       font_weight='bold' if bold else 'normal')
        node.text = value

    def box(self, x, y, width, height, nested=False):
        element(self.background, 'rect', x=x, y=y, width=width, height=height,
                rx=7 if nested else 11, fill='white' if nested else '#F8FAFC',
                stroke='#B2BDCA' if nested else '#CCD4DE', stroke_width=1.6,
                stroke_dasharray='none' if nested else '7 5')

    def line(self, points, field=False):
        element(self.wires, 'polyline', points=' '.join(f'{x},{y}' for x,y in points),
                stroke_dasharray='5 4' if field else 'none')

    def icon(self, kind, x, y, width, height, name):
        node = deepcopy(self.icons[kind])
        node.attrib.update({'x': str(x-width/2), 'y': str(y-height/2),
                           'width': str(width), 'height': str(height),
                           'preserveAspectRatio': 'xMidYMid meet'})
        group = element(self.devices, 'g', id=f'device-{name}')
        group.append(node)
        self.node_names.append(name)
        self.device_positions[name] = (x, y, width, height)

    def scada(self, x, y):
        # The supplied set has no SCADA icon; use a small vector workstation.
        group = element(self.devices, 'g', id='device-scada', fill='white',
                        stroke=INK, stroke_width=3.5, stroke_linejoin='round')
        element(group, 'rect', x=x-37, y=y-29, width=74, height=48, rx=3)
        element(group, 'path', d=f'M{x-8},{y+19} V{y+31} H{x-23} '
                f'M{x+8},{y+19} V{y+31} H{x+23} M{x-23},{y+31} H{x+23}', fill='none')
        self.node_names.append('scada')

    def attack_marker(self, x, y, number, color, target=None):
        group = element(self.root, 'g', id=f'attack-point-{number}')
        if target is not None:
            tx, ty = target
            distance = math.hypot(tx-x, ty-y)
            ux, uy = (tx-x)/distance, (ty-y)/distance
            element(group, 'line', x1=x+17*ux, y1=y+17*uy,
                    x2=tx-7*ux, y2=ty-7*uy, stroke=color, stroke_width=2.6)
            element(group, 'polygon', fill=color,
                    points=f'{tx},{ty} {tx-10*ux-4.5*uy},{ty-10*uy+4.5*ux} '
                           f'{tx-10*ux+4.5*uy},{ty-10*uy-4.5*ux}')
        element(group, 'circle', cx=x, cy=y, r=17, fill='white',
                stroke=color, stroke_width=2.8)
        label = element(group, 'text', x=x, y=y+8,
                        font_family='DejaVu Sans, sans-serif', font_size=23,
                        font_weight='bold', text_anchor='middle', fill=color)
        label.text = str(number)


def draw(config, branches, output, asset_dir):
    d = Diagram(asset_dir)
    scada = config['scada']['name']
    sb = branches[scada]
    d.box(308, 14, 750, 146)
    d.line([(487, 66), (650, 66)])
    d.line([(718, 66), (882, 66)])
    d.scada(450, 66)
    d.text(450, 120, 'SCADA', 25, True)
    d.text(450, 148, sb['lan']['interfaces'][scada]['ip'].split('/')[0], 21)
    d.icon('switch', 684, 66, 72, 64, sb['switch'])
    d.text(684, 120, sb['switch'], 25, True)
    d.icon('router', 920, 66, 76, 66, sb['router'])
    d.text(920, 120, sb['router'], 25, True)
    d.text(920, 148, sb['lan']['interfaces'][sb['router']]['ip'].split('/')[0], 21)
    d.line([(920, 164), (920, 208)])
    d.text(1035, 191, sb['link']['subnet'], 21)
    d.icon('router', 920, 244, 86, 72, 'r0')
    d.text(994, 254, 'r0', 27, True)

    for j, plc in enumerate(config['plcs']):
        name = plc['name']
        x = 129 + 226*j
        b = branches[name]
        d.line([(920, 281), (x, 363), (x, 412)])
        d.text(x, 384, b['link']['subnet'], 20)
        d.box(x-105, 397, 210, 711)
        d.icon('router', x, 444, 72, 64, b['router'])
        d.text(x, 507, b['router'], 25, True)
        d.text(x, 535, b['lan']['interfaces'][b['router']]['ip'].split('/')[0], 21)
        d.line([(x, 544), (x, 577)])
        d.icon('switch', x, 610, 70, 64, b['switch'])
        d.text(x, 676, b['switch'], 25, True)
        d.line([(x, 685), (x, 715)])
        d.icon('plc', x, 750, 67, 67, name)
        d.text(x, 813, name, 26, True)
        d.text(x, 842, b['lan']['interfaces'][name]['ip'].split('/')[0], 21)
        tanks = [s for s in plc['sensors'] if s in config['initial_tank_values']]
        physical = [(s, 'tank') for s in tanks] + [
            (a, 'pump' if a.startswith('PU') else 'valve') for a in plc['actuators']]
        d.mapping[name] = [n for n, kind in physical]
        if physical:
            rows = (len(physical)+1)//2
            # A nested box groups only equipment mapped to this PLC.
            d.box(x-95, 875, 190, rows*74+11, nested=True)
            d.line([(x, 851), (x, 875)], field=True)
            for k, (device, kind) in enumerate(physical):
                offset = 55 if name == 'PLC4' else 45
                dx = x if len(physical) == 1 else x + (-offset if k % 2 == 0 else offset)
                dy = 905 + (k//2)*74
                d.icon(kind, dx, dy, 43, 43, device)
                d.text(dx, dy+45, device, 22)

    # Paper scenario locations are annotations; they add no network nodes.
    x4 = 129 + 226*next(i for i, p in enumerate(config['plcs']) if p['name'] == 'PLC4')
    d.attack_marker(920+(x4-920)*0.68, 281+(363-281)*0.68, 1, '#D13D3D')
    d.attack_marker(x4+65, 700, 2, '#D4AC00', target=(x4, 700))
    px, py, _, _ = d.device_positions['PU10']
    # Stop below the device label so the upward arrow does not obscure PU10.
    d.attack_marker(px, py+93, 3, '#256EAC', target=(px, py+52))

    # Check device/address labels separately from the three numeric markers.
    # No titles, explanations, bandwidth, delays or legends enter the figure.
    valid_names = set(d.node_names) | {'SCADA'}
    for node in d.labels:
        if node.tag != tag('text'):
            continue
        if node.text not in valid_names:
            ipaddress.ip_network(node.text, strict=False)
    expected = {n['name'] for group in config['network']['nodes'].values() for n in group}
    assert expected <= set(d.node_names)
    assert len(d.node_names) == len(set(d.node_names))
    svg = ET.tostring(d.root, encoding='utf-8', xml_declaration=True)
    assert not any(node.tag == tag('image') for node in d.root.iter())
    for ext in ('svg', 'pdf', 'png'):
        path = output / ext / f'{NAME}.{ext}'
        path.parent.mkdir(parents=True, exist_ok=True)
        if ext == 'svg':
            path.write_bytes(svg)
        elif ext == 'pdf':
            cairosvg.svg2pdf(bytestring=svg, write_to=str(path))
        else:
            width = round(210/25.4*600)
            cairosvg.svg2png(bytestring=svg, write_to=str(path), output_width=width,
                output_height=round(width*d.height/d.width))
            with Image.open(path) as image:
                image.save(path, dpi=(600,600))
        print(path)
    cairosvg.svg2png(bytestring=svg, write_to=str(output / f'{NAME}_preview.png'),
                    output_width=1600, output_height=round(1600*d.height/d.width))
    return d.mapping


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'examples/c_town/config.yaml')
    parser.add_argument('--output-dir', type=Path, default=ROOT/'figures')
    parser.add_argument('--asset-dir', type=Path, default=ROOT/'assets/network_devices')
    parser.add_argument('--paper-dir', type=Path)
    args = parser.parse_args()
    config, branches, edges = read_topology(args.config)
    mapping = draw(config, branches, args.output_dir, args.asset_dir)
    manifest = {'source': str(args.config.resolve()),
        'source_sha256': hashlib.sha256(args.config.read_bytes()).hexdigest(),
        'nodes': config['network']['nodes'], 'drawn_edges': edges,
        'plc_equipment_mapping': mapping,
        'icons': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(args.asset_dir.glob('*.svg'))},
        'visible_content': 'Device names, LAN interface IP addresses, backbone subnets and three circled attack numbers.',
        'attack_markers': {
            '1': {'type': 'DoS', 'target': 'r0-r4', 'color': '#D13D3D'},
            '2': {'type': 'MITM', 'target': 'SCADA-to-PLC4 downlink at the PLC4 LAN',
                  'color': '#D4AC00', 'arrow_target': 's4-PLC4 link', 'position': 'right'},
            '3': {'type': 'PLC logic injection', 'target': 'PLC4 OpenPLC program / PU10 control rule',
                  'color': '#256EAC', 'arrow_target': 'PU10 device label', 'position': 'below'},
        },
        'note': 'Nested boxes group mapped tanks and actuators; no hydraulic pipe connections are implied.'}
    (args.output_dir/f'{NAME}_manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    if args.paper_dir:
        for ext in ('pdf', 'svg', 'png'):
            dest = args.paper_dir/'figures'/ext/f'{NAME}.{ext}'
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(args.output_dir/ext/f'{NAME}.{ext}', dest)
    print(f'Verified: {len(edges)+1} network nodes, {len(edges)} edges, '
          f'{sum(map(len, mapping.values()))} mapped physical devices.')


if __name__ == '__main__':
    main()
