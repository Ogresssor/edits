package studio

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"
)

type Quality struct {
	SHA         string            `json:"sha256"`
	Passed      bool              `json:"passed"`
	FullDecode  bool              `json:"full_decode"`
	Demo        bool              `json:"demonstration"`
	HumanReview bool              `json:"human_video_audio_review"`
	Black       []string          `json:"black_events"`
	Freeze      []string          `json:"freeze_events"`
	Loudness    map[string]string `json:"loudness"`
	Warnings    []string          `json:"warnings"`
	Duration    float64           `json:"duration"`
	Elapsed     float64           `json:"elapsed_seconds"`
}
type Artifact struct {
	SHA        string  `json:"sha256"`
	Path       string  `json:"path"`
	Story      string  `json:"story"`
	Revision   string  `json:"revision"`
	QA         Quality `json:"qa"`
	Reviewer   string  `json:"approved_by,omitempty"`
	ApprovedAt string  `json:"approved_at,omitempty"`
}
type QueueEntry struct {
	SHA       string `json:"sha256"`
	Story     string `json:"story"`
	PublishAt string `json:"publish_at"`
	State     string `json:"state"`
}
type State struct {
	Artifacts map[string]Artifact `json:"artifacts"`
	Queue     []QueueEntry        `json:"queue"`
}
type Store struct{ Root string }

func NewStore(root string) (*Store, error) {
	abs, e := filepath.Abs(root)
	if e != nil {
		return nil, e
	}
	if e = os.MkdirAll(abs, 0755); e != nil {
		return nil, e
	}
	return &Store{abs}, nil
}

func Lock(root, key string) (func(), error) {
	if e := os.MkdirAll(filepath.Join(root, "locks"), 0755); e != nil {
		return nil, e
	}
	id, _ := Hash(key)
	path := filepath.Join(root, "locks", id+".lock")
	f, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if e != nil {
		return nil, fmt.Errorf("job locked or interrupted; inspect %s: %w", path, e)
	}
	_, e = fmt.Fprintf(f, "pid=%d started=%s\n", os.Getpid(), time.Now().UTC().Format(time.RFC3339))
	closeErr := f.Close()
	if e != nil || closeErr != nil {
		os.Remove(path)
		return nil, errors.New("cannot write lock")
	}
	return func() { os.Remove(path) }, nil
}
func (s *Store) read() (State, error) {
	var state State
	e := ReadJSON(filepath.Join(s.Root, "state.json"), &state)
	if errors.Is(e, os.ErrNotExist) {
		state.Artifacts = map[string]Artifact{}
		return state, nil
	}
	if e != nil {
		return state, e
	}
	if state.Artifacts == nil {
		return state, errors.New("invalid state, refusing reset")
	}
	return state, nil
}
func (s *Store) mutate(fn func(*State) error) error {
	unlock, e := Lock(s.Root, "state")
	if e != nil {
		return e
	}
	defer unlock()
	state, e := s.read()
	if e != nil {
		return e
	}
	if e = fn(&state); e != nil {
		return e
	}
	return WriteJSON(filepath.Join(s.Root, "state.json"), state)
}
func (s *Store) Register(path, story, revision string, q Quality) error {
	digest, e := HashFile(path)
	if e != nil {
		return e
	}
	if digest != q.SHA || !q.Passed || !q.FullDecode || q.Demo {
		return errors.New("artifact not eligible after QA")
	}
	abs, e := filepath.Abs(path)
	if e != nil {
		return e
	}
	return s.mutate(func(state *State) error {
		if _, ok := state.Artifacts[digest]; !ok {
			state.Artifacts[digest] = Artifact{SHA: digest, Path: abs, Story: story, Revision: revision, QA: q}
		}
		return nil
	})
}
func (s *Store) Approve(digest, reviewer string, watched bool) error {
	if !watched || reviewer == "" {
		return errors.New("complete human video/audio review required")
	}
	return s.mutate(func(state *State) error {
		a, ok := state.Artifacts[digest]
		if !ok || !a.QA.Passed || a.QA.Demo {
			return errors.New("artifact not eligible")
		}
		actual, e := HashFile(a.Path)
		if e != nil || actual != digest {
			return errors.New("missing or changed export")
		}
		a.Reviewer = reviewer
		a.ApprovedAt = time.Now().UTC().Format(time.RFC3339)
		state.Artifacts[digest] = a
		return nil
	})
}
func (s *Store) Enqueue(digest string, when time.Time) error {
	msk := when.In(time.FixedZone("MSK", 3*3600))
	if !when.After(time.Now()) || (msk.Hour() != 9 && msk.Hour() != 13 && msk.Hour() != 17) || msk.Minute() != 0 || msk.Second() != 0 || msk.Nanosecond() != 0 {
		return errors.New("future slot 09:00/13:00/17:00 Moscow required")
	}
	return s.mutate(func(state *State) error {
		a, ok := state.Artifacts[digest]
		if !ok || !a.QA.Passed || a.Reviewer == "" || a.QA.Demo {
			return errors.New("exact approved export required")
		}
		actual, e := HashFile(a.Path)
		if e != nil || actual != digest {
			return errors.New("changed export")
		}
		utc := when.UTC().Format(time.RFC3339)
		for _, q := range state.Queue {
			if q.SHA == digest || q.Story == a.Story || q.PublishAt == utc {
				return errors.New("duplicate story, export or slot")
			}
		}
		state.Queue = append(state.Queue, QueueEntry{digest, a.Story, utc, "planned"})
		return nil
	})
}
func (s *Store) Queue() ([]QueueEntry, error) {
	state, e := s.read()
	if e != nil {
		return nil, e
	}
	sort.Slice(state.Queue, func(i, j int) bool { return state.Queue[i].PublishAt < state.Queue[j].PublishAt })
	if state.Queue == nil {
		state.Queue = []QueueEntry{}
	}
	return state.Queue, nil
}
func (s *Store) Invalidate(digest string, q Quality) error {
	return s.mutate(func(state *State) error {
		if a, ok := state.Artifacts[digest]; ok {
			a.QA = q
			a.Reviewer = ""
			a.ApprovedAt = ""
			state.Artifacts[digest] = a
		}
		for i := range state.Queue {
			if state.Queue[i].SHA == digest {
				state.Queue[i].State = "blocked_qa"
			}
		}
		return nil
	})
}
func (s *Store) SourceHash(path string) (string, error) {
	abs, e := filepath.Abs(path)
	if e != nil {
		return "", e
	}
	stat, e := os.Stat(abs)
	if e != nil {
		return "", e
	}
	key, _ := Hash(abs)
	cache := filepath.Join(s.Root, "source-cache", key+".json")
	var old struct {
		Size     int64  `json:"size"`
		Modified int64  `json:"mtime_ns"`
		SHA      string `json:"sha256"`
	}
	if ReadJSON(cache, &old) == nil && old.Size == stat.Size() && old.Modified == stat.ModTime().UnixNano() {
		return old.SHA, nil
	}
	sha, e := HashFile(abs)
	if e != nil {
		return "", e
	}
	old.Size = stat.Size()
	old.Modified = stat.ModTime().UnixNano()
	old.SHA = sha
	return sha, WriteJSON(cache, old)
}
