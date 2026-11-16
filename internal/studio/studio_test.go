package studio

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fixturePlan() Plan {
	return Plan{ID: "story-1", Source: "movie.mp4", SceneMap: "map.json", Rating: "strong", Story: Story{"Hook", "Development", "Payoff", "Context", "Reason"}, Segments: []Segment{{0, 3}}, Captions: []Caption{{Text: "Это тест.", Start: .1, End: 1.5, Highlight: "тест."}}}
}
func TestPlanSafety(t *testing.T) {
	cases := []struct {
		name   string
		change func(*Plan)
	}{
		{"path traversal", func(p *Plan) { p.ID = "../../secrets" }},
		{"missing payoff", func(p *Plan) { p.Story.Payoff = "" }},
		{"reversed interval", func(p *Plan) { p.Segments = []Segment{{2, 1}} }},
		{"overlap", func(p *Plan) { p.Segments = []Segment{{0, 2}, {1, 3}} }},
		{"negative start", func(p *Plan) { p.Segments = []Segment{{-1, 2}} }},
		{"caption injection", func(p *Plan) { p.Captions[0].Text = `{\fs999}bad` }},
		{"caption outside edit", func(p *Plan) { p.Captions[0].End = 9 }},
		{"wrong highlight", func(p *Plan) { p.Captions[0].Highlight = "missing" }},
		{"unknown motion", func(p *Plan) { p.TextMotion = "random" }},
		{"reveal without words", func(p *Plan) { p.TextMotion = "reveal" }},
		{"unlicensed music", func(p *Plan) { p.Music = &Music{Path: "track.mp3"} }},
		{"oversized zoom", func(p *Plan) {
			p.Effects = []Effect{{Kind: "punch_in", Start: 0, End: 1, Amount: .5, Reason: "answer"}}
		}},
		{"focus outside image", func(p *Plan) {
			p.Effects = []Effect{{Kind: "focus", Start: 0, End: 1, Box: []float64{.9, .9, .5, .5}, Reason: "detail"}}
		}},
		{"accent without reason", func(p *Plan) { p.Effects = []Effect{{Kind: "punch_in", Start: 0, End: 1}} }},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			p := fixturePlan()
			c.change(&p)
			if p.Validate() == nil {
				t.Fatal("unsafe plan accepted")
			}
		})
	}
	if e := fixturePlan().Validate(); e != nil {
		t.Fatal(e)
	}
}
func TestWordRevealIsTimed(t *testing.T) {
	p := fixturePlan()
	p.TextMotion = "reveal"
	p.Captions[0].Words = []Word{{"Это", .2, .7}, {"тест.", .7, 1.5}}
	if e := p.Validate(); e != nil {
		t.Fatal(e)
	}
	ass, e := Captions(p, "accent", 1080, 1920)
	if e != nil {
		t.Fatal(e)
	}
	if !strings.Contains(ass, "Dialogue: 1,0:00:00.20,0:00:00.70") || !strings.Contains(ass, `\alpha&HFF&`) {
		t.Fatal("word reveal shown too early or not masked")
	}
	p.Captions[0].Words[1].Text = "wrong"
	if p.Validate() == nil {
		t.Fatal("mismatched transcript accepted")
	}
}
func TestFullSceneMap(t *testing.T) {
	m := SceneMap{SHA: "abc", Complete: true, Scenes: []Segment{{0, 2}, {2, 5}}}
	if e := m.Validate("abc", 5); e != nil {
		t.Fatal(e)
	}
	m.Scenes[1].Start = 3
	if m.Validate("abc", 5) == nil {
		t.Fatal("gap accepted")
	}
	m.Scenes[1].Start = 2
	if m.Validate("other", 5) == nil || m.Validate("abc", 10) == nil {
		t.Fatal("wrong source/partial scan accepted")
	}
}
func setupStore(t *testing.T) (*Store, string, string, Quality) {
	t.Helper()
	s, e := NewStore(t.TempDir())
	if e != nil {
		t.Fatal(e)
	}
	file := filepath.Join(s.Root, "video.mp4")
	if e = os.WriteFile(file, []byte("export bytes"), 0600); e != nil {
		t.Fatal(e)
	}
	sha, e := HashFile(file)
	if e != nil {
		t.Fatal(e)
	}
	q := Quality{SHA: sha, Passed: true, FullDecode: true}
	if e = s.Register(file, "one-story", "revision", q); e != nil {
		t.Fatal(e)
	}
	return s, file, sha, q
}
func slot() time.Time {
	now := time.Now().In(time.FixedZone("MSK", 10800)).AddDate(0, 0, 7)
	return time.Date(now.Year(), now.Month(), now.Day(), 9, 0, 0, 0, now.Location())
}
func TestApprovalAndQueue(t *testing.T) {
	t.Run("no implicit approval", func(t *testing.T) {
		s, _, sha, _ := setupStore(t)
		if s.Enqueue(sha, slot()) == nil || s.Approve(sha, "user", false) == nil {
			t.Fatal("review gate bypassed")
		}
	})
	t.Run("changed file", func(t *testing.T) {
		s, path, sha, _ := setupStore(t)
		if e := s.Approve(sha, "user", true); e != nil {
			t.Fatal(e)
		}
		os.WriteFile(path, []byte("changed"), 0600)
		if s.Enqueue(sha, slot()) == nil {
			t.Fatal("changed file queued")
		}
	})
	t.Run("duplicate version", func(t *testing.T) {
		s, _, sha, _ := setupStore(t)
		s.Approve(sha, "user", true)
		if e := s.Enqueue(sha, slot()); e != nil {
			t.Fatal(e)
		}
		if s.Enqueue(sha, slot()) == nil {
			t.Fatal("duplicate queued")
		}
		file := filepath.Join(s.Root, "version.mp4")
		os.WriteFile(file, []byte("different revision"), 0600)
		other, _ := HashFile(file)
		q := Quality{SHA: other, Passed: true, FullDecode: true}
		s.Register(file, "one-story", "r2", q)
		s.Approve(other, "user", true)
		if s.Enqueue(other, slot().AddDate(0, 0, 1)) == nil {
			t.Fatal("same story queued twice")
		}
	})
	t.Run("failed recheck", func(t *testing.T) {
		s, _, sha, _ := setupStore(t)
		s.Approve(sha, "user", true)
		s.Enqueue(sha, slot())
		if e := s.Invalidate(sha, Quality{SHA: sha}); e != nil {
			t.Fatal(e)
		}
		q, _ := s.Queue()
		if q[0].State != "blocked_qa" || s.Approve(sha, "user", true) == nil {
			t.Fatal("failed QA kept approval")
		}
	})
	t.Run("demo and incomplete QA", func(t *testing.T) {
		s, path, _, q := setupStore(t)
		q.Demo = true
		if s.Register(path, "demo", "r2", q) == nil {
			t.Fatal("demo registered")
		}
		q.Demo = false
		q.FullDecode = false
		if s.Register(path, "partial", "r3", q) == nil {
			t.Fatal("incomplete QA accepted")
		}
	})
	t.Run("wrong slot", func(t *testing.T) {
		s, _, sha, _ := setupStore(t)
		s.Approve(sha, "user", true)
		if s.Enqueue(sha, slot().Add(time.Minute)) == nil || s.Enqueue(sha, time.Now().Add(-time.Hour)) == nil {
			t.Fatal("invalid schedule accepted")
		}
	})
}
func TestLockAndCache(t *testing.T) {
	s, path, _, _ := setupStore(t)
	unlock, e := Lock(s.Root, "same job")
	if e != nil {
		t.Fatal(e)
	}
	if release, e := Lock(s.Root, "same job"); e == nil {
		release()
		t.Fatal("concurrent writer accepted")
	}
	unlock()
	release, e := Lock(s.Root, "same job")
	if e != nil {
		t.Fatal(e)
	}
	release()
	first, _ := s.SourceHash(path)
	os.WriteFile(path, []byte("longer source bytes"), 0600)
	second, _ := s.SourceHash(path)
	if first == second {
		t.Fatal("stale source hash")
	}
}
func TestCorruptStateFailsClosed(t *testing.T) {
	s, e := NewStore(t.TempDir())
	if e != nil {
		t.Fatal(e)
	}
	os.WriteFile(filepath.Join(s.Root, "state.json"), []byte("broken"), 0600)
	if _, e = s.Queue(); e == nil {
		t.Fatal("corrupt state reset silently")
	}
}

func TestUnknownPlanFieldsFailClosed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "plan.json")
	b, _ := json.Marshal(fixturePlan())
	var fields map[string]any
	json.Unmarshal(b, &fields)
	fields["text_moton"] = "reveal"
	WriteJSON(path, fields)
	if _, e := ReadPlan(path); e == nil || !strings.Contains(e.Error(), "unknown field") {
		t.Fatal("misspelled field ignored")
	}
}

func TestTinyFocusCannotProduceZeroCrop(t *testing.T) {
	filters, _ := EffectFilters([]Effect{{Kind: "focus", Start: 0, End: 1, Box: []float64{.9999, .9999, .0001, .0001}}}, 1080, 1920, 25)
	if strings.Contains(strings.Join(filters, ";"), "crop=0:") || !strings.Contains(strings.Join(filters, ";"), "crop=2:2:") {
		t.Fatal(filters)
	}
}
func TestMetadata(t *testing.T) {
	i, e := ParseInfo("Duration: 00:00:04.00, start: 0\nVideo: h264, yuv420p, 640x360, 25 fps\nAudio: aac")
	if e != nil || i.FPS != 25 || !i.Audio || i.Duration != 4 {
		t.Fatal(i, e)
	}
}

func TestFFmpegIntegration(t *testing.T) {
	if os.Getenv("EDITS_INTEGRATION") != "1" {
		t.Skip("set EDITS_INTEGRATION=1 for actual FFmpeg tests")
	}
	binary, e := FindFFmpeg()
	if e != nil {
		t.Fatal(e)
	}
	m := Media{binary, context.Background()}
	s, e := NewStore(t.TempDir())
	if e != nil {
		t.Fatal(e)
	}
	source := filepath.Join(s.Root, "source.mp4")
	_, e = m.Run("", time.Minute, "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=4", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source)
	if e != nil {
		t.Fatal(e)
	}
	sceneMap, e := Analyze(m, s, source)
	if e != nil {
		t.Fatal(e)
	}
	p := fixturePlan()
	p.Source = source
	p.SceneMap = sceneMap
	p.Music = &Music{source, "Fixture", "Sine", "local:test", "local:test"}
	for _, kind := range []string{"pop", "punch_in", "focus"} {
		t.Run(kind, func(t *testing.T) {
			candidate := p
			candidate.ID = "integration-" + kind
			candidate.Demonstration = true
			candidate.TextMotion = "pop"
			if kind == "punch_in" {
				candidate.Effects = []Effect{{Kind: kind, Start: .6, End: 1.4, Amount: .05, Reason: "test"}}
			}
			if kind == "focus" {
				candidate.Effects = []Effect{{Kind: kind, Start: .6, End: 1.4, Box: []float64{.1, .3, .6, .4}, Reason: "test"}}
			}
			file, e := Render(m, s, candidate, "accent", false)
			if e != nil {
				t.Fatal(e)
			}
			before, _ := os.Stat(file)
			cached, e := Render(m, s, candidate, "accent", false)
			after, _ := os.Stat(cached)
			if e != nil || !before.ModTime().Equal(after.ModTime()) {
				t.Fatal("cache miss", e)
			}
			q, e := Check(m, s, file)
			if e != nil || !q.Passed || !q.Demo {
				t.Fatal(q, e)
			}
		})
	}
	for _, name := range []string{"black", "silent", "damaged"} {
		t.Run(name, func(t *testing.T) {
			folder := filepath.Join(s.Root, name)
			os.MkdirAll(folder, 0755)
			file := filepath.Join(folder, "video.mp4")
			if name == "damaged" {
				os.WriteFile(file, []byte("incomplete export"), 0600)
			} else {
				video, audio := "testsrc2=size=320x568:rate=25:duration=3", "sine=frequency=440:duration=3"
				if name == "black" {
					video = "color=c=black:size=320x568:rate=25:duration=3"
				}
				if name == "silent" {
					audio = "anullsrc=r=48000:cl=mono"
				}
				_, e = m.Run("", time.Minute, "-y", "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio, "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", file)
				if e != nil {
					t.Fatal(e)
				}
			}
			sha, _ := HashFile(file)
			revision, _ := Hash("negative-fixture")
			sourceSHA, _ := Hash("synthetic-source")
			WriteJSON(filepath.Join(folder, "receipt.json"), Receipt{Schema: 1, SHA: sha, Duration: 3, Revision: revision, SourceSHA: sourceSHA, Plan: fixturePlan()})
			q, e := Check(m, s, file)
			if name != "damaged" && (e != nil || !q.FullDecode) {
				t.Fatal("negative fixture did not complete decode", e)
			}
			if name == "damaged" && (e == nil || !strings.Contains(e.Error(), "FFmpeg")) {
				t.Fatal("damaged fixture was not rejected by decoder", e)
			}
			if e == nil && q.Passed {
				t.Fatal("bad media accepted")
			}
		})
	}
}
