// Shared by the static guide and optional regenerate controls; no API dependency.
export const CAMERA_VOCABULARY = [
  { key: "angle", label: "Angle", options: [
    ["lowangle", "Look up at the subject to make it feel powerful."],
    ["highangle", "Look down at the subject to show its surroundings or vulnerability."],
    ["overhead", "Look straight down from above the scene."],
    ["pov", "See the scene through a character's eyes."],
    ["overtheshoulder", "Look past one person's shoulder toward another subject."],
    ["eyelevel", "Keep the camera at the subject's eye height."],
    ["dutch", "Tilt the horizon to suggest unease or tension."],
  ] },
  { key: "movement", label: "Movement", options: [
    ["dollyin", "Move the camera closer to the subject."],
    ["dollyout", "Move the camera away to reveal more of the scene."],
    ["tracking", "Move alongside a subject as it travels."],
    ["orbit", "Move in an arc around the subject."],
    ["handheld", "Use gentle human movement for an immediate, informal feel."],
    ["craneup", "Raise the camera to reveal the scene from higher up."],
    ["cranedown", "Lower the camera toward the subject or scene."],
    ["static", "Keep the camera fixed in one position."],
  ] },
  { key: "lighting", label: "Lighting", options: [
    ["goldenhour", "Use warm, low sunlight near sunrise or sunset."],
    ["bluehour", "Use cool twilight just before sunrise or after sunset."],
    ["moody", "Use deep shadows and selective light for a dramatic feel."],
    ["softlight", "Use diffuse light with gentle shadows."],
    ["rimlight", "Light the subject's edges to separate it from the background."],
    ["silhouette", "Show a dark subject against a brighter background."],
    ["spotlight", "Focus a narrow beam of light on the subject."],
    ["backlight", "Place the main light behind the subject."],
  ] },
  { key: "composition", label: "Composition", options: [
    ["leadinglines", "Use lines in the scene to guide the eye toward the subject."],
    ["ruleofthirds", "Place the subject near a third of the frame instead of the center."],
    ["symmetry", "Balance matching shapes on either side of the frame."],
    ["negativespace", "Leave open space around the subject to give it emphasis."],
    ["foreground", "Include something close to the camera to create depth."],
  ] },
];
