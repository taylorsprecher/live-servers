// Smoke test for the Pathways Editor starter: the app pumps, the seeded
// example pathway renders, and validation reports it as valid.

import 'package:flutter_test/flutter_test.dart';

import 'package:pathways_editor/main.dart';
import 'package:pathways_editor/pathway.dart';

void main() {
  testWidgets('Editor loads with the seeded example pathway',
      (WidgetTester tester) async {
    await tester.pumpWidget(const PathwaysEditorApp());

    expect(find.text('Pathways Editor'), findsOneWidget);
    expect(find.text('Start'), findsWidgets);
    expect(find.text('Qualified?'), findsOneWidget);
    expect(find.text('Send email'), findsOneWidget);
  });

  test('Seeded example validates clean; a bare decision does not', () {
    final controller = PathwayController();
    controller.seedExample();
    expect(controller.nodes.length, 4);
    expect(controller.edges.length, 3);
    expect(controller.validate(), isEmpty);

    final broken = PathwayController();
    broken.addNode('decision', label: 'Qualified?');
    expect(broken.validate(), isNotEmpty);
  });
}
