// Pathways Editor — a Flutter starter for connectable pathways.
//
// Built from Taylor's Cross-Platform design document (Cross-Platform.gdoc,
// installed 2026-11-18), Option B: "Flutter app starter for a node-link
// editor." Drag nodes on the canvas, connect output ports to input
// ports, and validate the flow (missing inputs, cycles, unreachable
// nodes) before it runs. One codebase for iOS, Android, Web, macOS,
// Windows, and Linux; the fleet serves the Web build.

import 'package:material_ui/material_ui.dart';

import 'pathway.dart';

void main() {
  runApp(const PathwaysEditorApp());
}

class PathwaysEditorApp extends StatelessWidget {
  const PathwaysEditorApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Pathways Editor',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.indigo),
        useMaterial3: true,
      ),
      home: const EditorScreen(),
    );
  }
}

class EditorScreen extends StatefulWidget {
  const EditorScreen({super.key});

  @override
  State<EditorScreen> createState() => _EditorScreenState();
}

class _EditorScreenState extends State<EditorScreen> {
  final PathwayController _controller = PathwayController();
  String? _selectedId;
  bool _connectArmed = false;

  @override
  void initState() {
    super.initState();
    _controller.seedExample();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Color _typeColor(String type) {
    switch (type) {
      case 'start':
        return Colors.green.shade600;
      case 'decision':
        return Colors.amber.shade700;
      case 'output':
        return Colors.blueGrey.shade600;
      case 'action':
      default:
        return Colors.indigo.shade500;
    }
  }

  void _onNodeTap(PathwayNode node) {
    if (_connectArmed && _selectedId != null && node.id != _selectedId) {
      _attemptConnect(_controller.byId(_selectedId!)!, node);
      return;
    }
    setState(() {
      _selectedId = node.id;
      _connectArmed = false;
    });
  }

  Future<void> _attemptConnect(PathwayNode from, PathwayNode to) async {
    var outPort = from.outputPorts.isEmpty ? null : from.outputPorts.first;
    if (from.outputPorts.length > 1) {
      outPort = await showModalBottomSheet<String>(
        context: context,
        builder: (context) => SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ListTile(
                title: Text('Connect from which output of "${from.label}"?'),
              ),
              for (final port in from.outputPorts)
                ListTile(
                  leading: const Icon(Icons.output),
                  title: Text(port),
                  onTap: () => Navigator.pop(context, port),
                ),
            ],
          ),
        ),
      );
    }
    if (outPort == null || to.inputPorts.isEmpty) {
      setState(() => _connectArmed = false);
      return;
    }
    final ok = _controller.connect(
      from.id,
      outPort,
      to.id,
      to.inputPorts.first,
    );
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          ok
              ? 'Connected ${from.label} -> ${to.label}'
              : 'That connection is not allowed (duplicate or invalid ports).',
        ),
      ),
    );
    setState(() => _connectArmed = false);
  }

  void _validate() {
    final issues = _controller.validate();
    showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Validation'),
        content: issues.isEmpty
            ? const Text('Pathway is valid and ready to publish.')
            : Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [for (final issue in issues) Text('- $issue')],
              ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('OK'),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final selected =
        _selectedId == null ? null : _controller.byId(_selectedId!);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Pathways Editor'),
        actions: [
          IconButton(
            tooltip: 'Validate pathway',
            icon: const Icon(Icons.fact_check_outlined),
            onPressed: _validate,
          ),
          IconButton(
            tooltip: 'Reload example pathway',
            icon: const Icon(Icons.restart_alt),
            onPressed: () => setState(() {
              _controller.seedExample();
              _selectedId = null;
              _connectArmed = false;
            }),
          ),
          IconButton(
            tooltip: 'Clear canvas',
            icon: const Icon(Icons.delete_sweep_outlined),
            onPressed: () => setState(() {
              _controller.clear();
              _selectedId = null;
              _connectArmed = false;
            }),
          ),
        ],
      ),
      body: Column(
        children: [
          _toolbar(),
          Expanded(child: _canvas()),
          if (selected != null) _inspector(selected),
        ],
      ),
    );
  }

  Widget _toolbar() {
    return Material(
      elevation: 1,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
        child: SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: [
              const Text('Add node:'),
              const SizedBox(width: 8),
              for (final type in ['start', 'decision', 'action', 'output'])
                Padding(
                  padding: const EdgeInsets.only(right: 6),
                  child: OutlinedButton.icon(
                    icon: Icon(Icons.add_circle_outline,
                        size: 16, color: _typeColor(type)),
                    label: Text(type[0].toUpperCase() + type.substring(1)),
                    onPressed: () => _controller.addNode(type),
                  ),
                ),
              const SizedBox(width: 12),
              AnimatedBuilder(
                animation: _controller,
                builder: (context, _) => Text(
                  '${_controller.nodes.length} nodes - ${_controller.edges.length} edges',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _canvas() {
    return Container(
      color: const Color(0xFFF4F5FA),
      child: InteractiveViewer(
        constrained: false,
        boundaryMargin: const EdgeInsets.all(400),
        minScale: 0.3,
        maxScale: 2.0,
        child: SizedBox(
          width: 2400,
          height: 1600,
          child: AnimatedBuilder(
            animation: _controller,
            builder: (context, _) => Stack(
              children: [
                Positioned.fill(
                  child: CustomPaint(painter: EdgePainter(_controller)),
                ),
                for (final node in _controller.nodes) _nodeCard(node),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _nodeCard(PathwayNode node) {
    final isSelected = node.id == _selectedId;
    return Positioned(
      left: node.x,
      top: node.y,
      child: GestureDetector(
        onTap: () => _onNodeTap(node),
        onPanUpdate: (details) => _controller.moveNode(node.id, details.delta),
        child: Container(
          width: 152,
          decoration: BoxDecoration(
            color: Colors.white,
            borderRadius: BorderRadius.circular(10),
            border: Border.all(
              color: isSelected
                  ? Theme.of(context).colorScheme.primary
                  : Colors.grey.shade400,
              width: isSelected ? 2.5 : 1,
            ),
            boxShadow: const [
              BoxShadow(color: Colors.black12, blurRadius: 4, offset: Offset(0, 2)),
            ],
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                decoration: BoxDecoration(
                  color: _typeColor(node.type),
                  borderRadius:
                      const BorderRadius.vertical(top: Radius.circular(9)),
                ),
                child: Text(
                  node.type.toUpperCase(),
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 10,
                    fontWeight: FontWeight.bold,
                    letterSpacing: 1,
                  ),
                ),
              ),
              Padding(
                padding: const EdgeInsets.all(8),
                child: Text(
                  node.label,
                  style: const TextStyle(
                    fontSize: 14,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
                child: Text(
                  'in: ${node.inputPorts.isEmpty ? '-' : node.inputPorts.join(', ')}'
                  '  out: ${node.outputPorts.isEmpty ? '-' : node.outputPorts.join(', ')}',
                  style: TextStyle(fontSize: 10, color: Colors.grey.shade700),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _inspector(PathwayNode node) {
    return Material(
      elevation: 4,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        child: Row(
          children: [
            Icon(Icons.radio_button_checked, color: _typeColor(node.type)),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                '${node.label} - ${node.type}'
                '${_connectArmed ? ' - tap another node to connect it' : ''}',
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
            ),
            TextButton.icon(
              icon: const Icon(Icons.cable, size: 18),
              label: const Text('Connect to...'),
              onPressed: node.outputPorts.isEmpty
                  ? null
                  : () => setState(() => _connectArmed = true),
            ),
            TextButton.icon(
              icon: const Icon(Icons.delete_outline, size: 18),
              label: const Text('Delete'),
              onPressed: () {
                _controller.removeNode(node.id);
                setState(() {
                  _selectedId = null;
                  _connectArmed = false;
                });
              },
            ),
          ],
        ),
      ),
    );
  }
}
