// Connectable Pathways — graph model, controller, validation, edge painter.
//
// The model follows Taylor's Cross-Platform design document
// (Cross-Platform.gdoc, installed 2026-11-18): a pathway is a directed
// graph of nodes connected by edges between ports, with validation
// before execution. Node / Edge / Controller shapes are the document's
// own starter model, extended with move/delete and the document's
// validation rules (missing required inputs, cycle detection,
// unreachable nodes).

import 'package:material_ui/material_ui.dart';

/// A node in a pathway graph. [x]/[y] are canvas layout coordinates.
class PathwayNode {
  final String id;
  final String type; // start | decision | action | output
  String label;
  double x;
  double y;
  final List<String> inputPorts;
  final List<String> outputPorts;

  PathwayNode({
    required this.id,
    required this.type,
    required this.label,
    required this.x,
    required this.y,
    required this.inputPorts,
    required this.outputPorts,
  });

  Offset get center => Offset(x + 76, y + 30);

  /// Default ports per node type, per the design document's model.
  static (List<String>, List<String>) portsFor(String type) {
    switch (type) {
      case 'start':
        return (const <String>[], const <String>['out1']);
      case 'decision':
        return (const <String>['in1'], const <String>['yes', 'no']);
      case 'output':
        return (const <String>['in1'], const <String>[]);
      case 'action':
      default:
        return (const <String>['in1'], const <String>['out1']);
    }
  }
}

/// An edge from one node's output port to another node's input port.
class PathwayEdge {
  final String id;
  final String fromNodeId;
  final String fromPortId;
  final String toNodeId;
  final String toPortId;
  final String? condition;

  const PathwayEdge({
    required this.id,
    required this.fromNodeId,
    required this.fromPortId,
    required this.toNodeId,
    required this.toPortId,
    this.condition,
  });
}

/// Holds the graph and notifies listeners on every mutation.
class PathwayController extends ChangeNotifier {
  final List<PathwayNode> nodes = [];
  final List<PathwayEdge> edges = [];
  int _nodeSeq = 0;
  int _edgeSeq = 0;

  PathwayNode? byId(String id) {
    for (final n in nodes) {
      if (n.id == id) return n;
    }
    return null;
  }

  PathwayNode addNode(String type, {String? label, double? x, double? y}) {
    _nodeSeq++;
    final (inputs, outputs) = PathwayNode.portsFor(type);
    final node = PathwayNode(
      id: 'n$_nodeSeq',
      type: type,
      label: label ?? _defaultLabel(type),
      x: x ?? 80.0 + (nodes.length % 5) * 190.0,
      y: y ?? 80.0 + (nodes.length ~/ 5) * 140.0,
      inputPorts: List.of(inputs),
      outputPorts: List.of(outputs),
    );
    nodes.add(node);
    notifyListeners();
    return node;
  }

  String _defaultLabel(String type) {
    switch (type) {
      case 'start':
        return 'Start';
      case 'decision':
        return 'Qualified?';
      case 'output':
        return 'Output';
      case 'action':
      default:
        return 'Action';
    }
  }

  void moveNode(String id, Offset delta) {
    final node = byId(id);
    if (node == null) return;
    node.x = (node.x + delta.dx).clamp(0, 4000);
    node.y = (node.y + delta.dy).clamp(0, 4000);
    notifyListeners();
  }

  void removeNode(String id) {
    nodes.removeWhere((n) => n.id == id);
    edges.removeWhere((e) => e.fromNodeId == id || e.toNodeId == id);
    notifyListeners();
  }

  /// Connects [fromNodeId]'s [fromPortId] output to [toNodeId]'s
  /// [toPortId] input. Rejects self-connections and duplicates.
  bool connect(
    String fromNodeId,
    String fromPortId,
    String toNodeId,
    String toPortId,
  ) {
    if (fromNodeId == toNodeId) return false;
    final from = byId(fromNodeId);
    final to = byId(toNodeId);
    if (from == null || to == null) return false;
    if (!from.outputPorts.contains(fromPortId)) return false;
    if (!to.inputPorts.contains(toPortId)) return false;
    final duplicate = edges.any(
      (e) =>
          e.fromNodeId == fromNodeId &&
          e.fromPortId == fromPortId &&
          e.toNodeId == toNodeId &&
          e.toPortId == toPortId,
    );
    if (duplicate) return false;
    _edgeSeq++;
    edges.add(
      PathwayEdge(
        id: 'e$_edgeSeq',
        fromNodeId: fromNodeId,
        fromPortId: fromPortId,
        toNodeId: toNodeId,
        toPortId: toPortId,
      ),
    );
    notifyListeners();
    return true;
  }

  void clear() {
    nodes.clear();
    edges.clear();
    notifyListeners();
  }

  /// Seeds the design document's example pathway:
  /// Start -> Qualified? --yes--> Send email, --no--> Archive.
  void seedExample() {
    clear();
    final start = addNode('start', label: 'Start', x: 80, y: 160);
    final filter = addNode('decision', label: 'Qualified?', x: 340, y: 160);
    final notify = addNode('action', label: 'Send email', x: 620, y: 80);
    final archive = addNode('output', label: 'Archive', x: 620, y: 260);
    connect(start.id, 'out1', filter.id, 'in1');
    connect(filter.id, 'yes', notify.id, 'in1');
    connect(filter.id, 'no', archive.id, 'in1');
  }

  /// The design document's validation rules. Returns issue strings;
  /// an empty list means the pathway is valid.
  List<String> validate() {
    final issues = <String>[];

    // Missing required inputs: every non-start node needs an incoming edge.
    for (final node in nodes) {
      if (node.type == 'start') continue;
      final hasInput = edges.any((e) => e.toNodeId == node.id);
      if (!hasInput) {
        issues.add('${node.label} (${node.type}) is missing a required input.');
      }
    }

    // Cycle detection (DFS over the directed graph).
    final cycle = _findCycle();
    if (cycle != null) {
      issues.add('Cycle detected: ${cycle.join(' -> ')}');
    }

    // Unreachable nodes: BFS from every start node.
    final starts = nodes.where((n) => n.type == 'start').toList();
    if (starts.isEmpty && nodes.isNotEmpty) {
      issues.add('Pathway has no Start node.');
    } else {
      final seen = <String>{};
      final queue = starts.map((n) => n.id).toList();
      while (queue.isNotEmpty) {
        final id = queue.removeAt(0);
        if (!seen.add(id)) continue;
        for (final e in edges.where((e) => e.fromNodeId == id)) {
          queue.add(e.toNodeId);
        }
      }
      for (final node in nodes) {
        if (!seen.contains(node.id)) {
          issues.add('${node.label} (${node.type}) is unreachable from Start.');
        }
      }
    }

    return issues;
  }

  List<String>? _findCycle() {
    final state = <String, int>{}; // 0=unvisited 1=in stack 2=done
    final stack = <String>[];
    List<String>? found;

    void visit(String id) {
      if (found != null) return;
      state[id] = 1;
      stack.add(id);
      for (final e in edges.where((e) => e.fromNodeId == id)) {
        final next = e.toNodeId;
        if (state[next] == 1) {
          final start = stack.indexOf(next);
          found = [...stack.sublist(start), next]
              .map((nid) => byId(nid)?.label ?? nid)
              .toList();
          return;
        }
        if ((state[next] ?? 0) == 0) visit(next);
        if (found != null) return;
      }
      stack.removeLast();
      state[id] = 2;
    }

    for (final node in nodes) {
      if ((state[node.id] ?? 0) == 0) visit(node.id);
      if (found != null) break;
    }
    return found;
  }
}

/// Paints edges as bezier curves between node centers.
class EdgePainter extends CustomPainter {
  final PathwayController controller;

  EdgePainter(this.controller) : super(repaint: controller);

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = const Color(0xFF5B6ABF)
      ..strokeWidth = 2
      ..style = PaintingStyle.stroke;
    final dot = Paint()..color = const Color(0xFF5B6ABF);

    for (final edge in controller.edges) {
      final from = controller.byId(edge.fromNodeId);
      final to = controller.byId(edge.toNodeId);
      if (from == null || to == null) continue;
      final a = Offset(from.x + 152, from.y + 30);
      final b = Offset(to.x, to.y + 30);
      final path = Path()
        ..moveTo(a.dx, a.dy)
        ..cubicTo(a.dx + 60, a.dy, b.dx - 60, b.dy, b.dx, b.dy);
      canvas.drawPath(path, paint);
      canvas.drawCircle(b, 4, dot);
    }
  }

  @override
  bool shouldRepaint(covariant EdgePainter oldDelegate) => true;
}
