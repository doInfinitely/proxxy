import SwiftUI

/// List of user-created prompt templates.
struct TemplateListView: View {
    @State private var templates: [Template] = []
    @State private var showEditor = false
    @State private var selectedTemplate: Template?

    private let storage = StorageService.shared

    var body: some View {
        NavigationView {
            Group {
                if templates.isEmpty {
                    VStack(spacing: 12) {
                        Image(systemName: "doc.text")
                            .font(.system(size: 40))
                            .foregroundColor(.secondary)
                        Text("No templates yet")
                            .font(.headline)
                            .foregroundColor(.secondary)
                        Text("Create templates with {parameters} for quick tasks.")
                            .font(.subheadline)
                            .foregroundColor(.secondary)
                            .multilineTextAlignment(.center)
                            .padding(.horizontal)
                    }
                } else {
                    List {
                        ForEach(templates) { template in
                            Button(action: {
                                selectedTemplate = template
                            }) {
                                TemplateRow(template: template)
                            }
                            .buttonStyle(.plain)
                        }
                        .onDelete(perform: deleteTemplates)
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Templates")
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button(action: { showEditor = true }) {
                        Image(systemName: "plus")
                    }
                }
            }
            .sheet(isPresented: $showEditor) {
                TemplateEditorView { newTemplate in
                    templates.append(newTemplate)
                    storage.saveTemplates(templates)
                }
            }
            .sheet(item: $selectedTemplate) { template in
                TemplateFillView(template: template)
            }
            .onAppear {
                templates = storage.loadTemplates()
            }
        }
    }

    private func deleteTemplates(at offsets: IndexSet) {
        templates.remove(atOffsets: offsets)
        storage.saveTemplates(templates)
    }
}

struct TemplateRow: View {
    let template: Template

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(template.name)
                .font(.headline)
            Text(template.prompt)
                .font(.subheadline)
                .foregroundColor(.secondary)
                .lineLimit(2)
            if !template.parameterNames.isEmpty {
                HStack(spacing: 4) {
                    ForEach(template.parameterNames, id: \.self) { param in
                        Text("{\(param)}")
                            .font(.caption)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Color.blue.opacity(0.1))
                            .cornerRadius(4)
                    }
                }
            }
        }
        .padding(.vertical, 4)
    }
}
